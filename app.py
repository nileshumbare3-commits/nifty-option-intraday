from flask import Flask, render_template, request, redirect, url_for, session
import os
import json
from kiteconnect import KiteConnect

app = Flask(__name__)
app.secret_key = 'a-super-secret-key-that-is-static' # For session management

# Define the session file path
SESSION_FILE = "kite_session.json"

@app.route('/')
def index():
    # If a session file exists, we assume the user is "logged in"
    if os.path.exists(SESSION_FILE):
        return redirect(url_for('backtest_page'))
    # Otherwise, show the page to enter API credentials
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    # Store API key and secret in the server-side session for later use
    session['api_key'] = request.form.get('api_key')
    session['api_secret'] = request.form.get('api_secret')
    
    # Create a KiteConnect instance and get the login URL
    kite = KiteConnect(api_key=session['api_key'])
    return redirect(kite.login_url())

@app.route('/kite_callback')
def callback():
    request_token = request.args.get('request_token')
    if not request_token:
        return "Error: request_token not found. Could not authenticate.", 400

    # Retrieve API key and secret from the session
    api_key = session.get('api_key')
    api_secret = session.get('api_secret')
    if not api_key or not api_secret:
        return "Error: API credentials not found in session. Please try logging in again.", 400

    # Generate the session and access token
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        
        # Save the access token and other data to the session file for persistence
        with open(SESSION_FILE, 'w') as f:
            json.dump(data, f)
            
        # Redirect to the main backtesting page
        return redirect(url_for('backtest_page'))
    except Exception as e:
        return f"Authentication failed: {str(e)}", 400

@app.route('/run_backtest', methods=['POST'])
def run_backtest():
    # Step 1: Initialize KiteConnect client from saved session
    try:
        with open(SESSION_FILE, 'r') as f:
            session_data = json.load(f)
        # Use the API key from the session for correctness
        kite = KiteConnect(api_key=session_data.get('api_key'))
        kite.set_access_token(session_data["access_token"])
    except Exception as e:
        return f"Failed to initialize Kite client. Please log in again. Error: {str(e)}", 400

    # Step 2: Get parameters from form
    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    stop_loss = float(request.form.get('stop_loss'))
    target_profit = float(request.form.get('target_profit'))
    # instrument = request.form.get('instrument') # For future use

    # Step 3: Main backtesting loop
    from datetime import datetime, timedelta
    import pandas as pd
    import pandas_ta as ta

    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()

    trade_log = []
    all_days = pd.date_range(start=start_date, end=end_date, freq='B')

    # Cache for instruments to avoid fetching every time
    try:
        instruments_list = kite.instruments("NFO")
    except Exception as e:
        return f"Could not fetch instruments: {str(e)}", 500
    
    instruments_df = pd.DataFrame(instruments_list)

    for day in all_days:
        try:
            # --- Helper logic to find the correct Nifty Future instrument ---
            current_month_fut = instruments_df[
                (instruments_df['name'] == 'NIFTY') &
                (instruments_df['instrument_type'] == 'FUT') &
                (instruments_df['expiry'] >= pd.to_datetime(day))
            ].sort_values('expiry').iloc[0]
            
            instrument_token = current_month_fut['instrument_token']
            
            # --- Fetch 5-min historical data ---
            from_date = day.strftime('%Y-%m-%d') + " 09:15:00"
            to_date = day.strftime('%Y-%m-%d') + " 15:30:00"
            
            data = kite.historical_data(instrument_token, from_date, to_date, "5minute")
            if not data:
                continue # Skip if no data (e.g. holiday)

            df = pd.DataFrame(data)
            df['date'] = pd.to_datetime(df['date'])

            # --- Calculate AVWAP and Bands ---
            # Anchor to the first 5-min candle (9:15)
            anchor_time = pd.to_datetime(day.strftime('%Y-%m-%d') + " 09:15:00").tz_localize('Asia/Kolkata')
            anchor_df = df[df['date'] >= anchor_time]
            
            if anchor_df.empty:
                continue

            # Calculate Anchored VWAP manually using an expanding window
            anchor_df['price_vol'] = anchor_df['close'] * anchor_df['volume']
            anchor_df['cum_price_vol'] = anchor_df['price_vol'].expanding().sum()
            anchor_df['cum_vol'] = anchor_df['volume'].expanding().sum()
            anchor_df['vwap'] = anchor_df['cum_price_vol'] / anchor_df['cum_vol']

            # Calculate running Standard Deviation for bands
            anchor_df['sq_diff'] = (anchor_df['close'] - anchor_df['vwap'])**2
            anchor_df['variance'] = anchor_df['sq_diff'].expanding().mean()
            anchor_df['std_dev'] = anchor_df['variance'].pow(0.5)
            
            anchor_df['upper_band'] = anchor_df['vwap'] + anchor_df['std_dev']
            anchor_df['lower_band'] = anchor_df['vwap'] - anchor_df['std_dev']

            # --- Find Breakout and Simulate Trade ---
            trade_triggered = False
            for i, row in anchor_df.iterrows():
                if trade_triggered:
                    break
                
                if row['date'].time() > datetime.strptime("09:30", "%H:%M").time():
                    trade_details = None
                    # Bullish breakout -> Sell Put Spread
                    if row['close'] > row['upper_band']:
                        trade_details = simulate_spread_trade(
                            kite, instruments_df, day, row, 'PE', 
                            stop_loss, target_profit
                        )
                        trade_triggered = True
                    # Bearish breakout -> Sell Call Spread
                    elif row['close'] < row['lower_band']:
                        trade_details = simulate_spread_trade(
                            kite, instruments_df, day, row, 'CE', 
                            stop_loss, target_profit
                        )
                        trade_triggered = True
                    
                    if trade_details:
                        trade_log.append(trade_details)
        
        except Exception as e:
            print(f"Error processing {day.strftime('%Y-%m-%d')}: {str(e)}")

    results = {
        "parameters": {
            "start_date": start_date_str,
            "end_date": end_date_str,
            "stop_loss": stop_loss,
            "target_profit": target_profit,
        },
        "trade_log": trade_log,
        "summary": calculate_summary_stats(trade_log)
    }
    return render_template('results.html', results=results)

def get_next_weekly_expiry(trade_date, instruments_df):
    """Finds the next weekly expiry date from the trade date."""
    trade_date = pd.to_datetime(trade_date)
    nifty_options = instruments_df[
        (instruments_df['name'] == 'NIFTY') & 
        (instruments_df['instrument_type'] == 'CE')
    ]
    future_expiries = nifty_options[nifty_options['expiry'] > trade_date]['expiry'].unique()
    return sorted(future_expiries)[0]

def simulate_spread_trade(kite, instruments_df, trade_day, breakout_row, option_type, sl, tp):
    LOT_SIZE = 50
    STRIKE_DIFFERENCE = 300
    
    breakout_price = breakout_row['close']
    breakout_time = breakout_row['date']

    expiry_date = get_next_weekly_expiry(trade_day, instruments_df)
    atm_strike = round(breakout_price / 50) * 50
    sell_strike = atm_strike
    buy_strike = (sell_strike - STRIKE_DIFFERENCE) if option_type == 'PE' else (sell_strike + STRIKE_DIFFERENCE)

    try:
        sell_leg = instruments_df[(instruments_df['expiry'] == expiry_date) & (instruments_df['strike'] == sell_strike) & (instruments_df['instrument_type'] == option_type)].iloc[0]
        buy_leg = instruments_df[(instruments_df['expiry'] == expiry_date) & (instruments_df['strike'] == buy_strike) & (instruments_df['instrument_type'] == option_type)].iloc[0]
    except IndexError:
        print(f"Could not find options for {trade_day.strftime('%Y-%m-%d')}")
        return None

    from_time = breakout_time
    to_time = pd.to_datetime(trade_day.strftime('%Y-%m-%d') + " 15:30:00").tz_localize('Asia/Kolkata')
    
    sell_data = pd.DataFrame(kite.historical_data(sell_leg['instrument_token'], from_time, to_time, "minute"))
    buy_data = pd.DataFrame(kite.historical_data(buy_leg['instrument_token'], from_time, to_time, "minute"))

    if sell_data.empty or buy_data.empty:
        return None

    sell_data['date'] = pd.to_datetime(sell_data['date'])
    buy_data['date'] = pd.to_datetime(buy_data['date'])
    trade_df = pd.merge(sell_data, buy_data, on='date', suffixes=('_sell', '_buy'))

    if trade_df.empty:
        return None

    entry_price_sell = trade_df['close_sell'].iloc[0]
    entry_price_buy = trade_df['close_buy'].iloc[0]
    initial_credit = (entry_price_sell - entry_price_buy)

    for i, tick in trade_df.iterrows():
        current_pnl = (initial_credit - (tick['close_sell'] - tick['close_buy'])) * LOT_SIZE
        if current_pnl <= -sl:
            return { "date": trade_day.strftime('%Y-%m-%d'), "trade": f"Sell {option_type} Spread ({sell_strike}/{buy_strike})", "entry_time": breakout_time.strftime('%H:%M:%S'), "exit_time": tick['date'].strftime('%H:%M:%S'), "pnl": round(current_pnl, 2), "reason_for_exit": "Stop-Loss Hit" }
        if current_pnl >= tp:
            return { "date": trade_day.strftime('%Y-%m-%d'), "trade": f"Sell {option_type} Spread ({sell_strike}/{buy_strike})", "entry_time": breakout_time.strftime('%H:%M:%S'), "exit_time": tick['date'].strftime('%H:%M:%S'), "pnl": round(current_pnl, 2), "reason_for_exit": "Target Profit Hit" }

    final_pnl = (initial_credit - (trade_df['close_sell'].iloc[-1] - trade_df['close_buy'].iloc[-1])) * LOT_SIZE
    return { "date": trade_day.strftime('%Y-%m-%d'), "trade": f"Sell {option_type} Spread ({sell_strike}/{buy_strike})", "entry_time": breakout_time.strftime('%H:%M:%S'), "exit_time": trade_df['date'].iloc[-1].strftime('%H:%M:%S'), "pnl": round(final_pnl, 2), "reason_for_exit": "End of Day" }

def calculate_summary_stats(trade_log):
    if not trade_log:
        return {"status": "No trades were executed."}
    total_pnl = sum(trade.get('pnl', 0) for trade in trade_log)
    wins = sum(1 for trade in trade_log if trade.get('pnl', 0) > 0)
    losses = sum(1 for trade in trade_log if trade.get('pnl', 0) < 0)
    total_trades = len(trade_log)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    return {
        "status": "Backtest Complete.",
        "total_pnl": f"₹{round(total_pnl, 2)}",
        "total_trades": total_trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": f"{round(win_rate, 2)}%"
    }


@app.route('/backtest')
def backtest_page():
    # This page will show the form to run the backtest.
    return render_template('backtest.html')

@app.route('/logout')
def logout():
    # Delete the session file
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    # Clear the flask session
    session.clear()
    return redirect(url_for('index'))

@app.route('/check_session')
def check_session():
    return session.copy()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8080)
