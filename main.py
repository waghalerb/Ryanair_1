# main.py

import os
import json
from datetime import datetime, date, timedelta, timezone
import pandas as pd
import io

# Matplotlib setup for a server environment
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

from ryanair import Ryanair
import calendar
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from zoneinfo import ZoneInfo
from google.cloud import storage

# --- Configuration ---
ORIGIN_CITY = 'DUB'
DESTINATION_CITIES = ['VIE', 'BTS']
PRICE_HEATMAP = {'min_price': 10, 'max_price': 75}
NOTIFICATION_CHECKS_INTERVAL = 6 # Every 6 hours

# --- Google Cloud & Email Secrets (read from environment variables) ---
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
EMAIL_TO = os.environ.get("EMAIL_TO")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME") 

# --- File Names in Cloud Storage ---
HTML_FILENAME = "index.html"
JSON_FILENAME = "prices.json"
HISTORY_FILENAME = "price_history.csv"
GRAPH_FILENAME = "price_history_graph.png"
RUN_COUNT_FILENAME = "run_count.txt"

# Initialize Google Cloud Storage client
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

# --- Helper Functions ---
def price_heatmap_styler(price):
    min_p, max_p = PRICE_HEATMAP['min_price'], PRICE_HEATMAP['max_price']
    if price <= min_p: return 'background-color: lightgreen'
    if price > max_p: return 'background-color: lightcoral'
    cmap = mcolors.LinearSegmentedColormap.from_list("", ["lightgreen", "yellow", "orange"])
    norm_price = (price - min_p) / (max_p - min_p)
    return f'background-color: {mcolors.to_hex(cmap(norm_price))}'

def get_flight_prices():
    api = Ryanair(currency="EUR")
    all_found_flights = []
    today = date.today()
    for week in range(52):
        for day_of_week in [0, 6]:
            flight_date = today + timedelta(weeks=week, days=(day_of_week - today.weekday() + 7) % 7)
            for dest in DESTINATION_CITIES:
                try:
                    flights = api.get_cheapest_flights(ORIGIN_CITY, flight_date, flight_date, destination_airport=dest)
                    if flights: all_found_flights.extend(flights)
                except Exception as e: print(f"Error fetching {ORIGIN_CITY}->{dest}: {e}")
        flight_date = today + timedelta(weeks=week, days=(4 - today.weekday() + 7) % 7)
        for origin in DESTINATION_CITIES:
            try:
                flights = api.get_cheapest_flights(origin, flight_date, flight_date, destination_airport=ORIGIN_CITY)
                if flights: all_found_flights.extend(flights)
            except Exception as e: print(f"Error fetching {origin}->{ORIGIN_CITY}: {e}")
    if not all_found_flights: return pd.DataFrame()
    flight_data_list = [{'From': f.origin, 'To': f.destination, 'DateTime': f.departureTime, 'Price': float(f.price)} for f in all_found_flights]
    df = pd.DataFrame(flight_data_list)
    df['DateTime'] = pd.to_datetime(df['DateTime'])
    return df

def send_email(subject, html_content):
    if not all([SENDGRID_API_KEY, EMAIL_TO, EMAIL_FROM]):
        print("Email secrets not configured. Skipping email.")
        return
    message = Mail(from_email=EMAIL_FROM, to_emails=EMAIL_TO, subject=subject, html_content=html_content)
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(f"Email sent successfully with status code: {response.status_code}")
    except Exception as e:
        print(f"Error sending email: {e}")

def generate_price_change_html(comparison_df):
    price_changed = comparison_df['Price'] != comparison_df['Previous Price']
    new_flights = comparison_df['Previous Price'].isna()
    changed_df = comparison_df[price_changed | new_flights].copy()
    if changed_df.empty:
        return None
    changed_df['Previous Price'].fillna('New Flight', inplace=True)
    changed_df['Date'] = pd.to_datetime(changed_df['DateStr']).dt.strftime('%A, %Y-%m-%d')
    email_df = changed_df[['Date', 'From', 'To', 'Price', 'Previous Price']]
    return email_df.to_html(index=False, border=1)

def generate_full_price_html(current_flights_df):
    if current_flights_df.empty:
        return "<p>No flights found on this run.</p>"
    df = current_flights_df.copy()
    df['Date'] = df['DateTime'].dt.strftime('%A, %Y-%m-%d')
    df = df[['Date', 'From', 'To', 'Price']].sort_values(by=['DateTime'])
    return df.to_html(index=False, border=1)

def generate_and_upload_graph(history_df):
    if history_df.empty or len(history_df) < 2:
        print("Not enough data to generate a graph.")
        return False
    history_df['Flight'] = history_df['From'] + ' to ' + history_df['To'] + ' on ' + pd.to_datetime(history_df['FlightDateTime']).dt.strftime('%Y-%m-%d')
    plt.style.use('seaborn-v0_8-whitegrid')
    fig, ax = plt.subplots(figsize=(12, 7))
    for flight_name, group in history_df.groupby('Flight'):
        if len(group) > 1:
            ax.plot(pd.to_datetime(group['CheckTime']), group['Price'], marker='o', linestyle='-', label=flight_name)
    ax.set_title('Ryanair Flight Price Evolution', fontsize=16)
    ax.set_xlabel('Date of Price Check', fontsize=12)
    ax.set_ylabel('Price (EUR)', fontsize=12)
    ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
    plt.xticks(rotation=45)
    plt.tight_layout()
    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format='png')
    img_buffer.seek(0)
    graph_blob = bucket.blob(GRAPH_FILENAME)
    graph_blob.upload_from_file(img_buffer, content_type='image/png')
    graph_blob.make_public()
    print(f"Graph uploaded to {graph_blob.public_url}")
    plt.close(fig)
    return True

def create_final_html_dashboard(all_flights_df, graph_available):
    calendar_html = "<h2>No flights were found during the last check.</h2>"
    if not all_flights_df.empty:
        calendar_html = "" # Reset
        flights_by_date = {date: group for date, group in all_flights_df.groupby(all_flights_df['DateTime'].dt.date)}
        today = date.today()
        for i in range(12):
            current_year, current_month = today.year + (today.month + i - 1) // 12, (today.month + i - 1) % 12 + 1
            cal = calendar.Calendar()
            calendar_html += "<div class='month-block'>"
            calendar_html += f"<h4>{calendar.month_name[current_month]} {current_year}</h4>"
            calendar_html += "<table class='compact-calendar'><thead><tr>"
            for day in ["M", "T", "W", "T", "F", "S", "S"]: calendar_html += f"<th>{day}</th>"
            calendar_html += "</tr></thead><tbody>"
            for week in cal.monthdatescalendar(current_year, current_month):
                calendar_html += "<tr>"
                for day_date in week:
                    calendar_html += f"<td class='{'other-month' if day_date.month != current_month else ''}'>"
                    html_output_inner = "<table class='day-table'>"
                    if day_date in flights_by_date:
                        is_first = True
                        for _, flight in flights_by_date[day_date].iterrows():
                            booking_url = f"https://www.ryanair.com/ie/en/trip/flights/select?adults=1&dateOut={flight['DateTime']:%Y-%m-%d}&originIata={flight['From']}&destinationIata={flight['To']}&isReturn=false"
                            html_output_inner += f"<tr><td class='date-cell'>{day_date.day if is_first else ''}</td><td class='route-cell'>{flight['From']}→{flight['To']}:</td><td class='price-cell'><a href='{booking_url}' target='_blank' class='booking-link'><b>B</b></a><span class='flight-price' style='{price_heatmap_styler(flight['Price'])}'>€{flight['Price']:.0f}</span></td></tr>"
                            is_first = False
                    else:
                        html_output_inner += f"<tr><td class='date-cell'>{day_date.day}</td></tr>"
                    html_output_inner += "</table>"
                    calendar_html += html_output_inner + "</td>"
                calendar_html += "</tr>"
            calendar_html += "</tbody></table></div>"
    graph_html = "<h2>Price History Graph</h2>"
    if graph_available:
        graph_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{GRAPH_FILENAME}?t={int(time.time())}"
        graph_html += f"<img src='{graph_url}' alt='Price History Graph' style='width:100%; max-width:1000px;'>"
    else:
        graph_html += "<p>Graph will be generated once enough historical data is collected.</p>"
    return f"""
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ryanair Flight Dashboard</title><style>body{{font-family:Arial,sans-serif;margin:20px}}h1,.footer{{text-align:center}}.calendar-grid-container{{display:flex;flex-wrap:wrap;justify-content:space-around}}.month-block{{width:32%;min-width:300px;margin-bottom:15px}}.month-block h4{{text-align:center;margin:5px 0;font-size:14px}}.compact-calendar{{width:100%;border-collapse:collapse}}.compact-calendar th{{background-color:#f7f7f7;text-align:center;font-size:10px;border:1px solid #e0e0e0;padding:3px}}.compact-calendar td{{border:1px solid #e0e0e0;padding:2px;vertical-align:top;text-align:left;height:auto}}.other-month{{background-color:#fcfcfc}}.day-table{{width:100%;border-collapse:collapse}}.day-table td{{border:none;padding:1px 0;font-size:9px;white-space:nowrap}}.day-table .date-cell{{width:15px;font-weight:bold}}.day-table .route-cell{{padding-left:3px}}.day-table .price-cell{{text-align:right;width:50px}}.booking-link{{text-decoration:none;color:#007bff;font-size:10px;margin-right:3px}}.booking-link:hover{{text-decoration:underline}}.flight-price{{padding:1px 3px;border-radius:3px;color:#000;font-weight:bold}}</style>
    </head><body><h1>Ryanair Live Flight Dashboard</h1><div>{calendar_html}</div><hr><div>{graph_html}</div>
    <p style="text-align:center;font-size:12px;color:#888;">Last updated: {datetime.now(ZoneInfo("Europe/Dublin")).strftime('%Y-%m-%d %I:%M:%S %p %Z')}</p></body></html>
    """

# --- Main Cloud Function ---
def check_flights_and_update(request):
    """This is the main function that Google Cloud Scheduler will trigger."""
    print("--- Cloud Function Triggered ---")
    previous_prices_df, history_df = pd.DataFrame(), pd.DataFrame()
    try:
        blob = bucket.blob(JSON_FILENAME)
        previous_prices_df = pd.read_json(io.StringIO(blob.download_as_string().decode('utf-8')))
        print(f"Successfully loaded {JSON_FILENAME}")
    except Exception: print(f"No previous price file found.")
    try:
        blob = bucket.blob(HISTORY_FILENAME)
        history_df = pd.read_csv(io.StringIO(blob.download_as_string().decode('utf-8')))
        print(f"Successfully loaded {HISTORY_FILENAME}")
    except Exception: print(f"No history file found.")

    current_flights_df = get_flight_prices()
    
    if not current_flights_df.empty:
        if not previous_prices_df.empty:
            current_flights_df['DateStr'] = current_flights_df['DateTime'].dt.strftime('%Y-%m-%d')
            previous_prices_df['DateStr'] = pd.to_datetime(previous_prices_df['DateTime']).dt.strftime('%Y-%m-%d')
            comparison_df = pd.merge(current_flights_df, previous_prices_df[['DateStr', 'From', 'To', 'Price']], on=['DateStr', 'From', 'To'], how='left', suffixes=('', '_prev'))
            price_change_html = generate_price_change_html(comparison_df.rename(columns={'Price_prev': 'Previous Price'}))
            if price_change_html:
                print("Price changes detected. Sending alert email...")
                send_email("Ryanair Price Change Alert!", price_change_html)
        
        blob = bucket.blob(JSON_FILENAME)
        blob.upload_from_string(current_flights_df.to_json(orient='records'), content_type='application/json')
        print(f"Saved current prices to {JSON_FILENAME}")

        df_to_log = current_flights_df.copy()
        df_to_log['CheckTime'] = datetime.now(timezone.utc).isoformat()
        df_to_log.rename(columns={'DateTime': 'FlightDateTime'}, inplace=True)
        new_history_df = pd.concat([history_df, df_to_log[['CheckTime', 'FlightDateTime', 'From', 'To', 'Price']]], ignore_index=True)
        blob = bucket.blob(HISTORY_FILENAME)
        blob.upload_from_string(new_history_df.to_csv(index=False), content_type='text/csv')
        print(f"Updated {HISTORY_FILENAME}")
        history_df = new_history_df
    else:
        print("No flights found in current search.")

    run_count_blob = bucket.blob(RUN_COUNT_FILENAME)
    try: run_count = int(run_count_blob.download_as_string()) + 1
    except Exception: run_count = 1
    
    if run_count % NOTIFICATION_CHECKS_INTERVAL == 0:
        print(f"Run {run_count}: Sending 6-hour summary email.")
        summary_html = generate_full_price_html(current_flights_df)
        send_email("Ryanair 6-Hour Flight Price Summary", summary_html)
        run_count = 0 
    run_count_blob.upload_from_string(str(run_count))

    graph_available = generate_and_upload_graph(history_df)
    final_page_html = create_final_html_dashboard(current_flights_df, graph_available)
    blob = bucket.blob(HTML_FILENAME)
    blob.upload_from_string(final_page_html, content_type='text/html')
    blob.make_public()
    
    print(f"Dashboard updated. View it at: {blob.public_url}")
    print("--- Cloud Function Finished ---")
