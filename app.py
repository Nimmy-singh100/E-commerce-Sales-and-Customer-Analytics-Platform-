"""
STEP 2 - Streamlit dashboard.   Run:  streamlit run app.py
Needs output/olist_master_clean.csv (created by 1_clean_data.py).
"""
import calendar
import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Olist E-commerce Dashboard", page_icon="🛒", layout="wide")


@st.cache_data
def load_data():
    return pd.read_csv("output/olist_master_clean.csv", parse_dates=["purchase_ts", "order_month"])


df = load_data()

# ------------------------------------------------------------ FILTERS
st.sidebar.header("Filters")
d_min, d_max = df["purchase_ts"].min().date(), df["purchase_ts"].max().date()
dr = st.sidebar.date_input("Date range", (d_min, d_max), min_value=d_min, max_value=d_max)
if not isinstance(dr, tuple) or len(dr) != 2:
    st.info("Select an end date to continue.")
    st.stop()
cats = st.sidebar.multiselect("Category (empty = all)", sorted(df["category"].unique()))
states = st.sidebar.multiselect("Region / state (empty = all)", sorted(df["customer_state"].unique()))

mask = (df["purchase_ts"] >= pd.Timestamp(dr[0])) & (df["purchase_ts"] < pd.Timestamp(dr[1]) + pd.Timedelta(days=1))
if cats:
    mask &= df["category"].isin(cats)
if states:
    mask &= df["customer_state"].isin(states)
f = df[mask]
if f.empty:
    st.warning("No data for these filters.")
    st.stop()

# one row per order
orders = f.groupby("order_id").agg(customer=("customer_unique_id", "first"), ts=("purchase_ts", "first"),
                                   month=("order_month", "first"), revenue=("revenue", "sum"),
                                   review=("review_score", "first"), delivery_days=("delivery_days", "first"),
                                   delay_days=("delay_days", "first"), state=("customer_state", "first")).reset_index()

st.title("🛒 Olist E-commerce: Sales & Customer Behavior")

# ------------------------------------------------------------ TOP ROW: KPI CARDS
total_rev, n_orders = orders["revenue"].sum(), len(orders)
repeat_pct = (orders.groupby("customer").size() > 1).mean() * 100
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Revenue", f"R$ {total_rev:,.0f}")
k2.metric("Orders", f"{n_orders:,}")
k3.metric("Avg Order Value", f"R$ {total_rev / n_orders:,.2f}")
k4.metric("Repeat Customers", f"{repeat_pct:.1f}%")
k5.metric("Avg Review Score", f"{orders['review'].mean():.2f} / 5")

# ------------------------------------------------------------ MIDDLE ROW
m = orders.groupby("month").agg(revenue=("revenue", "sum"), orders=("order_id", "size")).reset_index()
m["aov"] = m["revenue"] / m["orders"]

c1, c2, c3 = st.columns([1.3, 1, 1.2])
with c1:
    metric = st.selectbox("Monthly trend", ["revenue", "orders", "aov"], format_func=str.title)
    st.plotly_chart(px.line(m, x="month", y=metric, markers=True, title=f"Monthly {metric.title()}"),
                    use_container_width=True)
with c2:
    cat = f.groupby("category")["revenue"].sum().nlargest(10).sort_values().reset_index()
    st.plotly_chart(px.bar(cat, x="revenue", y="category", orientation="h", title="Top 10 Categories by Revenue"),
                    use_container_width=True)

geo = f.groupby("customer_state").agg(revenue=("revenue", "sum"), lat=("lat", "mean"), lng=("lng", "mean")).reset_index()
ops_state = orders.groupby("state").agg(orders=("order_id", "size"), avg_delivery=("delivery_days", "mean"),
                                        avg_review=("review", "mean")).reset_index()
geo = geo.merge(ops_state, left_on="customer_state", right_on="state").drop(columns="state")
with c3:
    fig = px.scatter_geo(geo, lat="lat", lon="lng", size="revenue", color="avg_delivery", hover_name="customer_state",
                         hover_data={"lat": False, "lng": False, "revenue": ":,.0f", "orders": True,
                                     "avg_review": ":.2f", "avg_delivery": ":.1f"},
                         color_continuous_scale="RdYlGn_r", scope="south america", size_max=40,
                         title="Revenue (size) & Delivery Days (color) by State")
    fig.update_geos(fitbounds="locations", showcountries=True)
    st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------ BOTTOM ROW
b1, b2 = st.columns(2)

# ---- RFM
snap = orders["ts"].max() + pd.Timedelta(days=1)
rfm = orders.groupby("customer").agg(last=("ts", "max"), frequency=("order_id", "size"), monetary=("revenue", "sum"))
rfm["recency"] = (snap - rfm["last"]).dt.days
with b1:
    if len(rfm) < 10:
        st.info("Not enough customers for RFM with these filters.")
    else:
        rfm["R"] = pd.qcut(rfm["recency"].rank(method="first"), 5, labels=[5, 4, 3, 2, 1]).astype(int)
        rfm["M"] = pd.qcut(rfm["monetary"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int)
        # Olist: ~97% of customers order once, so frequency is scored by order count, not quintiles
        rfm["F"] = np.select([rfm["frequency"] >= 3, rfm["frequency"] == 2], [5, 3], default=1)
        rfm["segment"] = np.select(
            [(rfm.R >= 4) & (rfm.F >= 3), rfm.F >= 3, rfm.R >= 4, rfm.R == 3, rfm.R == 2],
            ["Champions", "Loyal", "New / Recent", "Need Attention", "At Risk"], default="Lost")
        order = ["Champions", "Loyal", "New / Recent", "Need Attention", "At Risk", "Lost"]
        seg = (rfm.groupby("segment").agg(customers=("monetary", "size"), avg_spend=("monetary", "mean"),
                                          avg_recency_days=("recency", "mean")).reindex(order).dropna().reset_index())
        st.plotly_chart(px.bar(seg, x="segment", y="customers", color="segment", text="customers",
                               title="RFM Segments (customers)"), use_container_width=True)
        with st.expander("Segment details"):
            st.dataframe(seg.round(1), hide_index=True)

# ---- Cohort retention
with b2:
    first = orders.groupby("customer")["month"].min().rename("cohort")
    c = orders.join(first, on="customer")
    c["period"] = (c["month"].dt.year - c["cohort"].dt.year) * 12 + (c["month"].dt.month - c["cohort"].dt.month)
    piv = c.groupby(["cohort", "period"])["customer"].nunique().unstack()
    piv = piv[piv[0] >= 30]  # ignore tiny cohorts
    if piv.empty:
        st.info("Not enough customers for a cohort view with these filters.")
    else:
        ret = piv.div(piv[0], axis=0).mul(100).iloc[:, :13]
        ret.index = ret.index.strftime("%Y-%m")
        fig = px.imshow(ret, text_auto=".1f", aspect="auto", color_continuous_scale="Blues",
                        labels=dict(x="Months since first purchase", y="Cohort", color="% returned"),
                        title="Cohort Retention (% of customers who ordered again)")
        st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------ DEEP DIVE
st.divider()
t1, t2, t3, t4 = st.tabs(["📅 Seasonality", "📦 Product performance", "🚚 Operations", "🗺️ Geography"])

with t1:
    s1, s2 = st.columns(2)
    moy = m.assign(moy=m["month"].dt.month).groupby("moy")["revenue"].mean().reset_index()
    moy["month_name"] = moy["moy"].map(lambda i: calendar.month_abbr[i])
    s1.plotly_chart(px.bar(moy, x="month_name", y="revenue", title="Avg revenue by calendar month"),
                    use_container_width=True)
    dow = orders["ts"].dt.day_name().value_counts().reindex(list(calendar.day_name)).reset_index()
    dow.columns = ["day", "orders"]
    s2.plotly_chart(px.bar(dow, x="day", y="orders", title="Orders by weekday"), use_container_width=True)

with t2:
    prod = f.groupby("category").agg(revenue=("revenue", "sum"), units=("revenue", "size"),
                                     margin=("margin_proxy", "sum")).reset_index()
    prod["margin_pct"] = prod["margin"] / prod["revenue"] * 100
    big = prod[prod["units"] >= 50]
    st.plotly_chart(px.scatter(big, x="units", y="margin_pct", size="revenue", hover_name="category", log_x=True,
                               title="Volume vs margin proxy (price minus freight, % of revenue)"),
                    use_container_width=True)
    p1, p2 = st.columns(2)
    ranked = prod.sort_values("revenue", ascending=False)
    p1.subheader("Top 10 categories")
    p1.dataframe(ranked.head(10).round(1), hide_index=True)
    p2.subheader("Bottom 10 categories")
    p2.dataframe(ranked.tail(10).round(1), hide_index=True)
    st.caption("Olist has no cost data, so margin is a proxy: (price - freight) / price.")

with t3:
    ops = orders.dropna(subset=["review"]).copy()
    ops["delivery_status"] = pd.cut(ops["delay_days"], [-np.inf, 0, 3, 7, np.inf],
                                    labels=["On time / early", "1-3 days late", "4-7 days late", "8+ days late"])
    by = ops.groupby("delivery_status", observed=True).agg(avg_review=("review", "mean"), orders=("review", "size")).reset_index()
    o1, o2 = st.columns(2)
    o1.plotly_chart(px.bar(by, x="delivery_status", y="avg_review", text=by["avg_review"].round(2),
                           title="Avg review score vs delivery delay"), use_container_width=True)
    o2.plotly_chart(px.bar(by, x="delivery_status", y="orders", title="Orders per delivery status"),
                    use_container_width=True)
    if {"On time / early", "8+ days late"} <= set(by["delivery_status"].astype(str)):
        ok = by.loc[by["delivery_status"] == "On time / early", "avg_review"].iloc[0]
        late = by.loc[by["delivery_status"] == "8+ days late", "avg_review"].iloc[0]
        st.success(f"💡 Orders 8+ days late score {ok - late:.2f} stars lower ({late:.2f} vs {ok:.2f}) than on-time orders.")

with t4:
    st.dataframe(geo.rename(columns={"customer_state": "state"})[["state", "revenue", "orders", "avg_delivery", "avg_review"]]
                 .sort_values("revenue", ascending=False).round(2), hide_index=True, use_container_width=True)
