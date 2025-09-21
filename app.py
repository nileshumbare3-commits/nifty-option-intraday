from flask import Flask, render_template, request, redirect, url_for, session
import os
import json
from kiteconnect import KiteConnect
import pandas as pd
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'a-super-secret-key-that-is-static'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

SESSION_FILE = "kite_session.json"

@app.route('/')
def index():
    if os.path.exists(SESSION_FILE):
        return redirect(url_for('backtest_page'))
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    session['api_key'] = request.form.get('api_key')
    session['api_secret'] = request.form.get('api_secret')
    kite = KiteConnect(api_key=session['api_key'])
    return redirect(kite.login_url())

@app.route('/kite_callback')
def callback():
    request_token = request.args.get('request_token')
    if not request_token:
        return "Error: request_token not found.", 400

    api_key = session.get('api_key')
    api_secret = session.get('api_secret')
    if not api_key or not api_secret:
        return "Error: API credentials not found in session.", 400

    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        data['api_key'] = api_key # Save api_key for later use
        with open(SESSION_FILE, 'w') as f:
            json.dump(data, f)
        return redirect(url_for('backtest_page'))
    except Exception as e:
        return f"Authentication failed: {str(e)}", 400

def get_next_weekly_expiry(trade_date, instruments_df):
    trade_date = pd.to_datetime(trade_date)
    nifty_options = instruments_df[(instruments_df['name'] == 'NIFTY') & (instruments_df['instrument_type'] == 'CE')]
    future_expiries = nifty_options[nifty_options['expiry'] > trade_date]['expiry'].unique()
    return sorted(future_expiries)[0]

def simulate_spread_trade(kite, instruments_df, trade_day, breakout_row, option_type, sl, tp):
    LOT_SIZE = 50
    STRIKE_DIFFERENCE = 300
    breakout_price, breakout_time = breakout_row['close'], breakout_row['date']
    expiry_date = get_next_weekly_expiry(trade_day, instruments_df)
    atm_strike = round(breakout_price / 50) * 50
    sell_strike, buy_strike = atm_strike, (atm_strike - STRIKE_DIFFERENCE) if option_type == 'PE' else (atm_strike + STRIKE_DIFFERENCE)

    try:
        sell_leg = instruments_df[(instruments_df['expiry'] == expiry_date) & (instruments_df['strike'] == sell_strike) & (instruments_df['instrument_type'] == option_type)].iloc[0]
        buy_leg = instruments_df[(instruments_df['expiry'] == expiry_date) & (instruments_df['strike'] == buy_strike) & (instruments_df['instrument_type'] == option_type)].iloc[0]
    except IndexError:
        return None

    from_time = breakout_time
    to_time = pd.to_datetime(f"{trade_day.strftime('%Y-%m-%d')} 15:30:00").tz_localize('Asia/Kolkata')
    sell_data = pd.DataFrame(kite.historical_data(sell_leg['instrument_token'], from_time, to_time, "minute"))
    buy_data = pd.DataFrame(kite.historical_data(buy_leg['instrument_token'], from_time, to_time, "minute"))
    if sell_data.empty or buy_data.empty: return None

    sell_data['date'] = pd.to_datetime(sell_data['date'])
    buy_data['date'] = pd.to_datetime(buy_data['date'])
    trade_df = pd.merge(sell_data, buy_data, on='date', suffixes=('_sell', '_buy'))
    if trade_df.empty: return None

    initial_credit = trade_df['close_sell'].iloc[0] - trade_df['close_buy'].iloc[0]
    for _, tick in trade_df.iterrows():
        pnl = (initial_credit - (tick['close_sell'] - tick['close_buy'])) * LOT_SIZE
        if pnl <= -sl or pnl >= tp:
            reason = "Stop-Loss Hit" if pnl <= -sl else "Target Profit Hit"
            return {"date": trade_day.strftime('%Y-%m-%d'), "trade": f"Sell {option_type} Spread ({sell_strike}/{buy_strike})", "entry_time": breakout_time.strftime('%H:%M:%S'), "exit_time": tick['date'].strftime('%H:%M:%S'), "pnl": round(pnl, 2), "reason_for_exit": reason}

    final_pnl = (initial_credit - (trade_df['close_sell'].iloc[-1] - trade_df['close_buy'].iloc[-1])) * LOT_SIZE
    return {"date": trade_day.strftime('%Y-%m-%d'), "trade": f"Sell {option_type} Spread ({sell_strike}/{buy_strike})", "entry_time": breakout_time.strftime('%H:%M:%S'), "exit_time": trade_df['date'].iloc[-1].strftime('%H:%M:%S'), "pnl": round(final_pnl, 2), "reason_for_exit": "End of Day"}

def calculate_summary_stats(trade_log):
    if not trade_log: return {"status": "No trades were executed."}
    total_pnl = sum(t.get('pnl', 0) for t in trade_log)
    wins = sum(1 for t in trade_log if t.get('pnl', 0) > 0)
    total_trades = len(trade_log)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    return {"status": "Backtest Complete.", "total_pnl": f"₹{round(total_pnl, 2)}", "total_trades": total_trades, "winning_trades": wins, "losing_trades": total_trades - wins, "win_rate": f"{round(win_rate, 2)}%"}

@app.route('/run_backtest', methods=['POST'])
def run_backtest():
    try:
        with open(SESSION_FILE, 'r') as f: session_data = json.load(f)
        kite = KiteConnect(api_key=session_data.get('api_key'))
        kite.set_access_token(session_data["access_token"])
    except Exception: return "Failed to initialize Kite client. Please log in again.", 400

    start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
    end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()
    stop_loss, target_profit = float(request.form.get('stop_loss')), float(request.form.get('target_profit'))

    try:
        instruments_df = pd.DataFrame(kite.instruments("NFO"))
    except Exception as e: return f"Could not fetch instruments: {str(e)}", 500

    trade_log = []
    for day in pd.date_range(start=start_date, end=end_date, freq='B'):
        try:
            fut = instruments_df[(instruments_df['name'] == 'NIFTY') & (instruments_df['instrument_type'] == 'FUT') & (instruments_df['expiry'] >= pd.to_datetime(day))].sort_values('expiry').iloc[0]
            data = kite.historical_data(fut['instrument_token'], f"{day} 09:15:00", f"{day} 15:30:00", "5minute")
            if not data: continue

            df = pd.DataFrame(data)
            df['date'] = pd.to_datetime(df['date'])
            anchor_df = df[df['date'] >= pd.to_datetime(f"{day} 09:15:00").tz_localize('Asia/Kolkata')].copy()
            if anchor_df.empty: continue

            anchor_df['vwap'] = (anchor_df['close'] * anchor_df['volume']).expanding().sum() / anchor_df['volume'].expanding().sum()
            anchor_df['std_dev'] = ((anchor_df['close'] - anchor_df['vwap'])**2).expanding().mean()**0.5
            anchor_df['upper_band'], anchor_df['lower_band'] = anchor_df['vwap'] + anchor_df['std_dev'], anchor_df['vwap'] - anchor_df['std_dev']

            trade_triggered = False
            for _, row in anchor_df.iterrows():
                if trade_triggered: break
                if row['date'].time() > datetime.strptime("09:30", "%H:%M").time():
                    trade_details = None
                    if row['close'] > row['upper_band']:
                        trade_details, trade_triggered = simulate_spread_trade(kite, instruments_df, day, row, 'PE', stop_loss, target_profit), True
                    elif row['close'] < row['lower_band']:
                        trade_details, trade_triggered = simulate_spread_trade(kite, instruments_df, day, row, 'CE', stop_loss, target_profit), True
                    if trade_details: trade_log.append(trade_details)
        except Exception as e:
            print(f"Error on {day.strftime('%Y-%m-%d')}: {e}")

    return render_template('results.html', results={"parameters": request.form.to_dict(), "trade_log": trade_log, "summary": calculate_summary_stats(trade_log)})

@app.route('/backtest')
def backtest_page():
    return render_template('backtest.html')

@app.route('/logout')
def logout():
    if os.path.exists(SESSION_FILE): os.remove(SESSION_FILE)
    session.clear()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8080)
