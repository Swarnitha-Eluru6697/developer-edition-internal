"""InAiEra Workspace service (data plane).

A FastAPI service that provides:
  - File-based workspace authoring (SQL, ipynb) per user
  - Git integration (GitHub, GitLab, Azure DevOps)
  - SQL execution via Data Lake House
  - Notebook execution via JupyterHub
  - Deploy-as-pipeline via dbt Runner

Runs inside the customer's data plane. Authenticated via JWT minted by the
control plane Root Platform App; tokens validated locally via JWKS.
"""
__version__ = "0.1.0"
