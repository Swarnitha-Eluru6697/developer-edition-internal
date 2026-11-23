"""Alerts: failures of the Workspace's jobs (dbt / SQL / Python runs, Flink jobs, runs Dagster refused) raised by rules and posted to
Slack / Microsoft Teams channels.

  kinds.py     what a rule can watch for, and the rules every org starts with
  engine.py    the rules themselves: observations in, breaches out; and the open / clear bookkeeping (pure, no I/O)
  sources.py   where observations come from (the dbt Runner, Flink, Dagster)
  webhooks.py  channel URL checks and the Slack / Teams messages
  service.py   the database side: sweep, raise, deliver, acknowledge, resolve
  sweeper.py   the background loop that runs a sweep every minute
"""
