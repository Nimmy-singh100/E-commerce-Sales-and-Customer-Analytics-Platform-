/* =====================================================================
   OLIST E-COMMERCE PROJECT  |  SQL (SQLite) -> export CSVs -> Tableau
   ---------------------------------------------------------------------
   SETUP (free): install "DB Browser for SQLite", create a new database,
   then File > Import > Table from CSV and import each Kaggle file with
   these table names:
       olist_orders_dataset.csv               -> orders
       olist_order_items_dataset.csv          -> order_items
       olist_customers_dataset.csv            -> customers
       olist_products_dataset.csv             -> products
       olist_order_reviews_dataset.csv        -> reviews
       olist_geolocation_dataset.csv          -> geolocation
       product_category_name_translation.csv  -> category_translation
   (tick "Column names in first line"; if the first column of a file shows
    odd characters, rename it to product_category_name / order_id / etc.)
   Then open the "Execute SQL" tab and run this file section by section.
   Using MySQL/PostgreSQL? Change: julianday() -> DATEDIFF / date subtraction,
   DATE(x,'start of month') -> DATE_TRUNC / DATE_FORMAT.
   ===================================================================== */


/* ================= STEP 1: CLEAN EACH TABLE ======================== */

-- 1a. Orders: remove duplicates, keep delivered, drop missing / impossible dates
DROP TABLE IF EXISTS clean_orders;
CREATE TABLE clean_orders AS
SELECT order_id, customer_id,
       order_purchase_timestamp       AS purchase_ts,
       order_delivered_customer_date  AS delivered_ts,
       order_estimated_delivery_date  AS estimated_ts
FROM orders
WHERE rowid IN (SELECT MIN(rowid) FROM orders GROUP BY order_id)          -- duplicates
  AND order_status = 'delivered'
  AND NULLIF(order_purchase_timestamp, '')      IS NOT NULL               -- missing values
  AND NULLIF(order_delivered_customer_date, '') IS NOT NULL
  AND NULLIF(order_estimated_delivery_date, '') IS NOT NULL
  AND julianday(order_delivered_customer_date) >= julianday(order_purchase_timestamp);  -- impossible dates

-- 1b. Items: remove duplicates, missing and non-positive prices
DROP TABLE IF EXISTS clean_items;
CREATE TABLE clean_items AS
SELECT order_id, order_item_id, product_id,
       CAST(price AS REAL)         AS price,
       CAST(freight_value AS REAL) AS freight_value
FROM order_items
WHERE rowid IN (SELECT MIN(rowid) FROM order_items GROUP BY order_id, order_item_id)
  AND price IS NOT NULL AND CAST(price AS REAL) > 0
  AND freight_value IS NOT NULL AND CAST(freight_value AS REAL) >= 0;

-- 1c. Customers
DROP TABLE IF EXISTS clean_customers;
CREATE TABLE clean_customers AS
SELECT customer_id, customer_unique_id, customer_city, customer_state,
       CAST(customer_zip_code_prefix AS INTEGER) AS zip
FROM customers
WHERE rowid IN (SELECT MIN(rowid) FROM customers GROUP BY customer_id)
  AND NULLIF(customer_unique_id, '') IS NOT NULL
  AND NULLIF(customer_state, '') IS NOT NULL;

-- 1d. Products: English category name, missing -> 'unknown'
DROP TABLE IF EXISTS clean_products;
CREATE TABLE clean_products AS
SELECT p.product_id,
       REPLACE(COALESCE(NULLIF(t.product_category_name_english, ''),
                        NULLIF(p.product_category_name, ''), 'unknown'), '_', ' ') AS category
FROM products p
LEFT JOIN category_translation t ON t.product_category_name = p.product_category_name
WHERE p.rowid IN (SELECT MIN(rowid) FROM products GROUP BY product_id);

-- 1e. Reviews: keep only the latest valid review per order
DROP TABLE IF EXISTS clean_reviews;
CREATE TABLE clean_reviews AS
SELECT order_id, review_score FROM (
    SELECT order_id, CAST(review_score AS INTEGER) AS review_score,
           ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY review_answer_timestamp DESC) AS rn
    FROM reviews
    WHERE CAST(review_score AS INTEGER) BETWEEN 1 AND 5
)
WHERE rn = 1;

-- 1f. Geolocation: one lat/lng per zip prefix, only points inside Brazil
DROP TABLE IF EXISTS clean_geo;
CREATE TABLE clean_geo AS
SELECT CAST(geolocation_zip_code_prefix AS INTEGER) AS zip,
       AVG(geolocation_lat) AS lat, AVG(geolocation_lng) AS lng
FROM geolocation
WHERE geolocation_lat BETWEEN -34 AND 6 AND geolocation_lng BETWEEN -74 AND -34
GROUP BY 1;


/* ================= STEP 2: ONE MASTER TABLE ======================== */
DROP TABLE IF EXISTS master;
CREATE TABLE master AS
SELECT i.order_id, i.order_item_id,
       ROW_NUMBER() OVER (PARTITION BY i.order_id ORDER BY i.order_item_id) AS item_seq,  -- 1 = first item of order
       c.customer_unique_id, c.customer_state, c.customer_city,
       o.purchase_ts,
       DATE(o.purchase_ts, 'start of month')                         AS order_month,
       COALESCE(p.category, 'unknown')                               AS category,
       i.price                                                       AS revenue,
       i.freight_value,
       ROUND(i.price - i.freight_value, 2)                           AS margin_proxy,  -- no cost data in Olist
       ROUND(julianday(o.delivered_ts) - julianday(o.purchase_ts), 2) AS delivery_days,
       ROUND(julianday(o.delivered_ts) - julianday(o.estimated_ts), 2) AS delay_days,  -- >0 = late
       r.review_score, g.lat, g.lng
FROM clean_items i
JOIN clean_orders o      ON o.order_id = i.order_id
JOIN clean_customers c   ON c.customer_id = o.customer_id
LEFT JOIN clean_products p ON p.product_id = i.product_id
LEFT JOIN clean_reviews r  ON r.order_id = i.order_id
LEFT JOIN clean_geo g      ON g.zip = c.zip;

-- Missing coordinates -> average of the customer's state
UPDATE master
SET lat = (SELECT AVG(m2.lat) FROM master m2 WHERE m2.customer_state = master.customer_state),
    lng = (SELECT AVG(m2.lng) FROM master m2 WHERE m2.customer_state = master.customer_state)
WHERE lat IS NULL;


/* ================= STEP 3: REMOVE OUTLIERS (IQR x 3) ================
   Whole orders are removed so order value / AOV stay correct.
   Set the multiplier 3 -> 1.5 for stricter removal, or skip this step.   */
DROP TABLE IF EXISTS order_stats;
CREATE TEMP TABLE order_stats AS
SELECT order_id, SUM(revenue) AS order_value, MAX(delivery_days) AS delivery_days
FROM master GROUP BY order_id;

DROP TABLE IF EXISTS q;
CREATE TEMP TABLE q AS
SELECT
 (SELECT order_value FROM (SELECT order_value, ROW_NUMBER() OVER (ORDER BY order_value) rn FROM order_stats)
   WHERE rn = (SELECT CAST(COUNT(*) * 0.25 AS INT) + 1 FROM order_stats)) AS v1,
 (SELECT order_value FROM (SELECT order_value, ROW_NUMBER() OVER (ORDER BY order_value) rn FROM order_stats)
   WHERE rn = (SELECT CAST(COUNT(*) * 0.75 AS INT) + 1 FROM order_stats)) AS v3,
 (SELECT delivery_days FROM (SELECT delivery_days, ROW_NUMBER() OVER (ORDER BY delivery_days) rn FROM order_stats)
   WHERE rn = (SELECT CAST(COUNT(*) * 0.25 AS INT) + 1 FROM order_stats)) AS d1,
 (SELECT delivery_days FROM (SELECT delivery_days, ROW_NUMBER() OVER (ORDER BY delivery_days) rn FROM order_stats)
   WHERE rn = (SELECT CAST(COUNT(*) * 0.75 AS INT) + 1 FROM order_stats)) AS d3;

SELECT * FROM q;   -- check the quartiles

DELETE FROM master WHERE order_id IN (
    SELECT s.order_id FROM order_stats s, q
    WHERE s.order_value   > q.v3 + 3 * (q.v3 - q.v1)
       OR s.delivery_days > q.d3 + 3 * (q.d3 - q.d1));


/* ================= STEP 4: DATA QUALITY CHECKS ===================== */
SELECT COUNT(*) AS rows_, COUNT(DISTINCT order_id) AS orders,
       COUNT(DISTINCT customer_unique_id) AS customers,
       MIN(purchase_ts) AS first_order, MAX(purchase_ts) AS last_order FROM master;

SELECT SUM(revenue IS NULL) AS null_revenue, SUM(category IS NULL) AS null_category,
       SUM(delivery_days IS NULL) AS null_delivery, SUM(lat IS NULL) AS null_lat,
       SUM(review_score IS NULL) AS null_review_ok   -- orders without review are expected
FROM master;

SELECT order_id, order_item_id, COUNT(*) AS n FROM master
GROUP BY 1, 2 HAVING n > 1;                          -- should return 0 rows


/* ================= STEP 5: ANALYSIS TABLES ========================= */

-- (1) SALES TRENDS: revenue, orders, AOV by month
DROP TABLE IF EXISTS monthly_sales;
CREATE TABLE monthly_sales AS
SELECT order_month,
       ROUND(SUM(revenue), 2)                                   AS revenue,
       COUNT(DISTINCT order_id)                                 AS orders,
       ROUND(SUM(revenue) / COUNT(DISTINCT order_id), 2)        AS aov
FROM master GROUP BY order_month ORDER BY order_month;

-- month-over-month growth
SELECT order_month, revenue,
       ROUND(100.0 * (revenue - LAG(revenue) OVER (ORDER BY order_month))
             / LAG(revenue) OVER (ORDER BY order_month), 1) AS mom_growth_pct
FROM monthly_sales;

-- seasonality: average revenue by calendar month (full years only if possible)
SELECT STRFTIME('%m', order_month) AS month_no, ROUND(AVG(revenue), 0) AS avg_revenue
FROM monthly_sales GROUP BY 1 ORDER BY 1;

-- (2) PRODUCT PERFORMANCE: revenue, volume, margin proxy
DROP TABLE IF EXISTS category_perf;
CREATE TABLE category_perf AS
SELECT category,
       ROUND(SUM(revenue), 2)                                  AS revenue,
       COUNT(*)                                                AS units,
       COUNT(DISTINCT order_id)                                AS orders,
       ROUND(100.0 * SUM(margin_proxy) / SUM(revenue), 1)      AS margin_pct,
       RANK() OVER (ORDER BY SUM(revenue) DESC)                AS revenue_rank
FROM master GROUP BY category;

SELECT * FROM category_perf ORDER BY revenue DESC LIMIT 10;      -- top 10
SELECT * FROM category_perf ORDER BY revenue ASC  LIMIT 10;      -- bottom 10

-- (3) RFM SEGMENTATION
DROP TABLE IF EXISTS rfm;
CREATE TABLE rfm AS
WITH cust AS (
    SELECT customer_unique_id,
           MAX(purchase_ts)          AS last_ts,
           COUNT(DISTINCT order_id)  AS frequency,
           ROUND(SUM(revenue), 2)    AS monetary
    FROM master GROUP BY customer_unique_id
),
base AS (
    SELECT *, CAST(julianday((SELECT DATE(MAX(purchase_ts), '+1 day') FROM master)) - julianday(last_ts) AS INT) AS recency
    FROM cust
),
scored AS (
    SELECT *,
           NTILE(5) OVER (ORDER BY recency DESC, customer_unique_id) AS r,   -- 5 = most recent
           NTILE(5) OVER (ORDER BY monetary, customer_unique_id)     AS m,
           CASE WHEN frequency >= 3 THEN 5 WHEN frequency = 2 THEN 3 ELSE 1 END AS f  -- ~97% order once
    FROM base
)
SELECT customer_unique_id, recency, frequency, monetary, r, f, m,
       CASE WHEN r >= 4 AND f >= 3 THEN 'Champions'
            WHEN f >= 3            THEN 'Loyal'
            WHEN r >= 4            THEN 'New / Recent'
            WHEN r = 3             THEN 'Need Attention'
            WHEN r = 2             THEN 'At Risk'
            ELSE 'Lost' END AS segment
FROM scored;

SELECT segment, COUNT(*) AS customers, ROUND(AVG(monetary), 1) AS avg_spend,
       ROUND(AVG(recency), 0) AS avg_recency_days
FROM rfm GROUP BY segment ORDER BY customers DESC;

-- (4) COHORT RETENTION
DROP TABLE IF EXISTS cohort;
CREATE TABLE cohort AS
WITH o AS (SELECT DISTINCT customer_unique_id, order_id, order_month FROM master),
first_order AS (SELECT customer_unique_id, MIN(order_month) AS cohort_month FROM o GROUP BY 1),
act AS (
    SELECT f.cohort_month, o.customer_unique_id,
           (CAST(SUBSTR(o.order_month, 1, 4) AS INT) - CAST(SUBSTR(f.cohort_month, 1, 4) AS INT)) * 12
           + CAST(SUBSTR(o.order_month, 6, 2) AS INT) - CAST(SUBSTR(f.cohort_month, 6, 2) AS INT) AS period
    FROM o JOIN first_order f USING (customer_unique_id)
)
SELECT SUBSTR(cohort_month, 1, 7) AS cohort_month, period,
       COUNT(DISTINCT customer_unique_id) AS customers,
       ROUND(100.0 * COUNT(DISTINCT customer_unique_id)
             / (SELECT COUNT(*) FROM first_order f2 WHERE f2.cohort_month = act.cohort_month), 2) AS retention_pct
FROM act GROUP BY cohort_month, period
HAVING (SELECT COUNT(*) FROM first_order f2 WHERE f2.cohort_month = act.cohort_month) >= 30;  -- skip tiny cohorts

-- repeat customer %
SELECT ROUND(100.0 * SUM(frequency > 1) / COUNT(*), 2) AS repeat_customer_pct FROM rfm;

-- (5) OPERATIONS: delivery delay vs review score
DROP TABLE IF EXISTS delivery_vs_review;
CREATE TABLE delivery_vs_review AS
WITH o AS (
    SELECT order_id, MAX(delay_days) AS delay_days, MAX(review_score) AS review_score
    FROM master GROUP BY order_id
)
SELECT CASE WHEN delay_days <= 0 THEN '1 On time / early'
            WHEN delay_days <= 3 THEN '2 1-3 days late'
            WHEN delay_days <= 7 THEN '3 4-7 days late'
            ELSE '4 8+ days late' END AS delivery_status,
       COUNT(*) AS orders, ROUND(AVG(review_score), 2) AS avg_review
FROM o WHERE review_score IS NOT NULL
GROUP BY 1 ORDER BY 1;

SELECT * FROM delivery_vs_review;

-- (6) GEOGRAPHY: sales and delivery performance by state
DROP TABLE IF EXISTS state_perf;
CREATE TABLE state_perf AS
WITH o AS (
    SELECT order_id, customer_state, SUM(revenue) AS revenue, MAX(delivery_days) AS delivery_days,
           MAX(delay_days) AS delay_days, MAX(review_score) AS review_score,
           AVG(lat) AS lat, AVG(lng) AS lng
    FROM master GROUP BY order_id, customer_state
)
SELECT customer_state, ROUND(SUM(revenue), 2) AS revenue, COUNT(*) AS orders,
       ROUND(SUM(revenue) / COUNT(*), 2) AS aov,
       ROUND(AVG(delivery_days), 1) AS avg_delivery_days,
       ROUND(100.0 * AVG(delay_days > 0), 1) AS pct_late,
       ROUND(AVG(review_score), 2) AS avg_review,
       AVG(lat) AS lat, AVG(lng) AS lng
FROM o GROUP BY customer_state ORDER BY revenue DESC;

-- KPI row in one query
SELECT ROUND(SUM(revenue), 0) AS total_revenue, COUNT(DISTINCT order_id) AS orders,
       ROUND(SUM(revenue) / COUNT(DISTINCT order_id), 2) AS aov,
       ROUND(AVG(CASE WHEN item_seq = 1 THEN review_score END), 2) AS avg_review
FROM master;


/* ================= STEP 6: EXPORT FOR TABLEAU =======================
   DB Browser: File > Export > Table(s) as CSV file, then pick:
   master, monthly_sales, category_perf, rfm, cohort,
   delivery_vs_review, state_perf                                        */
