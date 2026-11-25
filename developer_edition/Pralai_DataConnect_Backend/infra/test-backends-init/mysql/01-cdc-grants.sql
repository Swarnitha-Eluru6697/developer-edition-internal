-- Runs once, when the MySQL data volume is first created (the image executes /docker-entrypoint-initdb.d on an empty datadir).
-- The Debezium MySQL connector needs these as GLOBAL privileges (ON *.*); the per-database ALL PRIVILEGES that
-- MYSQL_USER gets on its own database is not enough. Without them the task fails or snapshots 0 rows.
GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT, LOCK TABLES ON *.* TO 'dataconnect'@'%';
FLUSH PRIVILEGES;
