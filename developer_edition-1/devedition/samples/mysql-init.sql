-- Developer Edition sample source: MySQL shop data for testing DataConnect's MySQL CDC. Test data only.

CREATE TABLE customers (
  customer_id INT AUTO_INCREMENT PRIMARY KEY,
  full_name   VARCHAR(120) NOT NULL,
  email       VARCHAR(200) NOT NULL UNIQUE,
  city        VARCHAR(80),
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE inventory (
  sku         VARCHAR(20) PRIMARY KEY,
  warehouse   VARCHAR(40) NOT NULL,
  on_hand     INT NOT NULL,
  updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

INSERT INTO customers (full_name, email, city) VALUES
  ('Lakshmi Nair', 'lakshmi.nair@example.com', 'Kochi'),
  ('Rahul Verma',  'rahul.verma@example.com',  'Jaipur'),
  ('Divya Menon',  'divya.menon@example.com',  'Chennai'),
  ('Imran Khan',   'imran.khan@example.com',   'Lucknow');

INSERT INTO inventory (sku, warehouse, on_hand) VALUES
  ('SKU-1001', 'BLR-1', 140), ('SKU-1002', 'BLR-1', 35), ('SKU-1003', 'HYD-2', 12),
  ('SKU-1004', 'HYD-2', 80),  ('SKU-1005', 'BOM-1', 22), ('SKU-1006', 'BOM-1', 60);

-- Debezium needs to read the binlog as this user.
GRANT SELECT, RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'sample'@'%';
FLUSH PRIVILEGES;
