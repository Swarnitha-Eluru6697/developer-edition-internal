"""Alerts: trouble with an organization's pipelines (a connector failed or degraded, a deploy failed, the error threshold paused it,
dead-letter errors, failing quality checks, ...) raised by rules and posted to its Slack / Microsoft Teams channels.

  kinds.py     what a rule can watch for, and the rules every organization starts with
  engine.py    the rules themselves: observations in, breaches out; and the open / keep / clear bookkeeping (pure, no I/O)
  sources.py   where observations come from: DataConnect's own tables (runtime state, operations, audit, failure counters, drift)
  webhooks.py  channel URL checks and the Slack / Teams messages
  links.py     the signed Acknowledge / Resolve links in those messages
  service.py   the database side: rules, channels, sweep, raise, deliver, acknowledge, resolve
  sweeper.py   the background loop that sweeps every organization every minute

Ported from the Workspace (InaiEra_workspace_backend/app/services/alerts); see docs/ALERTS_IMPLEMENTATION_GUIDE.md.
"""
