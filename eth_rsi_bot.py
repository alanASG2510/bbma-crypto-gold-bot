name: ETH RSI(14) Mean Reversion Alert Bot v6.1.1

on:
  schedule:
    - cron: '5 */4 * * *'
  workflow_dispatch:

env:
  ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION: true

jobs:
  alert-bot:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Restore cached state
        uses: actions/cache@v4
        with:
          path: eth_rsi_bot_state.json
          key: eth-rsi-state-${{ github.run_id }}
          restore-keys: |
            eth-rsi-state-

      - name: Install dependencies
        run: |
          pip install yfinance pandas numpy requests ccxt

      - name: Run ETH RSI(14) Mean Reversion Bot v6.1.1
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          TICKER: ETH-USD
          RSI_PERIOD: 14
          RSI_OVERSOLD: 30
          RSI_OVERBOUGHT: 70
          HEALTH_INTERVAL: 7
        run: python eth_rsi_bot.py

      - name: Save state to cache
        uses: actions/cache@v4
        with:
          path: eth_rsi_bot_state.json
          key: eth-rsi-state-${{ github.run_id }}

      - name: Upload state artifact
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: eth-rsi-state-${{ github.run_id }}
          path: |
            eth_rsi_bot_state.json
            eth_rsi_bot.log
          retention-days: 30

      - name: Upload logs on failure
        uses: actions/upload-artifact@v4
        if: failure()
        with:
          name: eth-rsi-logs-${{ github.run_id }}
          path: |
            eth_rsi_bot_state.json
            eth_rsi_bot_state.json.backup
            eth_rsi_bot.log
          retention-days: 7
