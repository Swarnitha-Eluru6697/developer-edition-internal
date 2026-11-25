"""Permission IDs the tests hand out (Workspace's permissions.json). Tokens carry these, never role names."""
EDIT = ["workspace:dbt:edit", "workspace:sql-jobs:edit", "workspace:python-jobs:edit"]
RUN = ["workspace:dbt:run", "workspace:sql-jobs:run", "workspace:python-jobs:run"]
ALL_JOB = EDIT + RUN
