"""
STEP 1 - Clean the Olist dataset and build ONE analysis-ready table.

Setup:  put the Kaggle CSVs in ./data  then run:  python 1_clean_data.py
Output: output/olist_master_clean.csv  +  output/cleaning_report.txt
"""
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path("data")
OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)

OUTLIER_K = 3.0  # IQR multiplier (3 = only extreme values). Set to None to keep all orders.
report = []


def log(step, before, after):
    msg = f"{step:<48}{before:>9,} -> {after:>9,}   removed {before - after:,}"
    print(msg)
    report.append(msg)


def read(name, **kw):
    return pd.read_csv(DATA_DIR / name, **kw)


# ---------------------------------------------------------------- ORDERS
date_cols = ["order_purchase_timestamp", "order_approved_at",
             "order_delivered_carrier_date", "order_delivered_customer_date",
             "order_estimated_delivery_date"]
orders = read("olist_orders_dataset.csv", parse_dates=date_cols)

n = len(orders); orders = orders.drop_duplicates("order_id")
log("orders: duplicate order_id", n, len(orders))

n = len(orders); orders = orders[orders["order_status"] == "delivered"]
log("orders: keep delivered only", n, len(orders))

n = len(orders)
orders = orders.dropna(subset=["order_purchase_timestamp", "order_delivered_customer_date",
                               "order_estimated_delivery_date"])
log("orders: missing key dates", n, len(orders))

n = len(orders)
orders = orders[orders["order_delivered_customer_date"] >= orders["order_purchase_timestamp"]]
log("orders: delivered before purchased (impossible)", n, len(orders))

orders = orders[["order_id", "customer_id", "order_purchase_timestamp",
                 "order_delivered_customer_date", "order_estimated_delivery_date"]]

# ---------------------------------------------------------------- ITEMS
items = read("olist_order_items_dataset.csv")
n = len(items); items = items.drop_duplicates(["order_id", "order_item_id"])
log("items: duplicate rows", n, len(items))

n = len(items)
items = items.dropna(subset=["order_id", "product_id", "price", "freight_value"])
items = items[(items["price"] > 0) & (items["freight_value"] >= 0)]
log("items: missing / non-positive price", n, len(items))
items = items[["order_id", "order_item_id", "product_id", "price", "freight_value"]]

# ---------------------------------------------------------------- CUSTOMERS
customers = read("olist_customers_dataset.csv").drop_duplicates("customer_id")
customers = customers.dropna(subset=["customer_unique_id", "customer_state", "customer_zip_code_prefix"])

# ---------------------------------------------------------------- PRODUCTS (+ English category names)
products = read("olist_products_dataset.csv")[["product_id", "product_category_name"]]
products = products.drop_duplicates("product_id")
translation = read("product_category_name_translation.csv").drop_duplicates("product_category_name")
products = products.merge(translation, on="product_category_name", how="left")
products["category"] = (products["product_category_name_english"]
                        .fillna(products["product_category_name"])
                        .fillna("unknown").str.replace("_", " ").str.title())

# ---------------------------------------------------------------- REVIEWS (keep latest review per order)
reviews = read("olist_order_reviews_dataset.csv", parse_dates=["review_answer_timestamp"])
reviews = reviews.dropna(subset=["review_score"])
reviews = reviews[reviews["review_score"].between(1, 5)]
reviews = (reviews.sort_values("review_answer_timestamp")
           .drop_duplicates("order_id", keep="last")[["order_id", "review_score"]])

# ---------------------------------------------------------------- GEOLOCATION (one lat/lng per zip prefix)
geo = read("olist_geolocation_dataset.csv").drop_duplicates()
geo = geo[geo["geolocation_lat"].between(-34, 6) & geo["geolocation_lng"].between(-74, -34)]  # inside Brazil
geo = (geo.groupby("geolocation_zip_code_prefix")
       .agg(lat=("geolocation_lat", "median"), lng=("geolocation_lng", "median"))
       .reset_index().rename(columns={"geolocation_zip_code_prefix": "customer_zip_code_prefix"}))

# ---------------------------------------------------------------- MERGE
df = (items.merge(orders, on="order_id", how="inner")
      .merge(customers, on="customer_id", how="inner")
      .merge(products[["product_id", "category"]], on="product_id", how="left")
      .merge(reviews, on="order_id", how="left")
      .merge(geo, on="customer_zip_code_prefix", how="left"))
df["category"] = df["category"].fillna("Unknown")

# ---------------------------------------------------------------- FEATURES
df["purchase_ts"] = df["order_purchase_timestamp"]
df["order_month"] = df["purchase_ts"].dt.to_period("M").dt.to_timestamp()
df["delivery_days"] = (df["order_delivered_customer_date"] - df["purchase_ts"]).dt.total_seconds() / 86400
df["delay_days"] = (df["order_delivered_customer_date"] - df["order_estimated_delivery_date"]).dt.total_seconds() / 86400
df["revenue"] = df["price"]
# Olist has no cost data, so margin is a PROXY: price minus freight paid
df["margin_proxy"] = df["price"] - df["freight_value"]

# missing coordinates -> median of the customer's state
df[["lat", "lng"]] = df.groupby("customer_state")[["lat", "lng"]].transform(lambda s: s.fillna(s.median()))

# ---------------------------------------------------------------- OUTLIERS (whole orders, so AOV stays valid)
if OUTLIER_K:
    per_order = df.groupby("order_id").agg(order_value=("price", "sum"),
                                           freight=("freight_value", "sum"),
                                           delivery_days=("delivery_days", "first"))
    bad = set()
    for col in per_order.columns:
        q1, q3 = per_order[col].quantile([0.25, 0.75])
        lo, hi = q1 - OUTLIER_K * (q3 - q1), q3 + OUTLIER_K * (q3 - q1)
        flagged = per_order.index[(per_order[col] < lo) | (per_order[col] > hi)]
        print(f"   outlier bounds {col}: {lo:,.1f} to {hi:,.1f} -> {len(flagged):,} orders")
        bad |= set(flagged)
    n = df["order_id"].nunique()
    df = df[~df["order_id"].isin(bad)]
    log("orders: IQR outliers (value/freight/delivery)", n, df["order_id"].nunique())

# ---------------------------------------------------------------- FINAL TABLE + CHECKS
cols = ["order_id", "order_item_id", "customer_unique_id", "customer_state", "customer_city",
        "purchase_ts", "order_month", "category", "price", "freight_value", "revenue",
        "margin_proxy", "delivery_days", "delay_days", "review_score", "lat", "lng"]
df = df[cols].round({"delivery_days": 2, "delay_days": 2, "lat": 4, "lng": 4}).reset_index(drop=True)

assert not df.duplicated(["order_id", "order_item_id"]).any(), "duplicates remain"
must_have = [c for c in cols if c not in ("review_score", "customer_city")]
assert not df[must_have].isna().any().any(), "unexpected missing values"
print(f"\nreview_score still missing for {df['review_score'].isna().mean():.2%} of rows "
      "(orders with no review - kept on purpose, ignored in review averages)")

df.to_csv(OUT_DIR / "olist_master_clean.csv", index=False)
(OUT_DIR / "cleaning_report.txt").write_text("\n".join(report))
print(f"\nSaved {len(df):,} rows / {df['order_id'].nunique():,} orders -> output/olist_master_clean.csv")
