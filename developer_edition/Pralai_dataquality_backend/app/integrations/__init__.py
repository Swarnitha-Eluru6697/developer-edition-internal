"""Outbound clients for UCS, the dbt runner and the FlinkSQL runner.

Every call authenticates either with
  * a freshly minted, short-lived, org-bound, audience+scope service JWT
    (app.core.service_auth) — service operations, or
  * the calling user's own bearer — user-delegated reads/creates on the
    runners, which then apply their own org + RBAC checks to that user.
No standing token, shared header secret or raw signing key is ever sent.
"""
