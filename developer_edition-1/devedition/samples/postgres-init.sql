-- Developer Edition sample source: a small shop database for testing DataConnect (CDC source,
-- JDBC sink target) and Workspace (Postgres connection). Test data only.
-- Started with: docker compose --profile samples up -d

CREATE TABLE customers (
  customer_id  serial PRIMARY KEY,
  full_name    text        NOT NULL,
  email        text        NOT NULL UNIQUE,
  city         text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
  product_id   serial PRIMARY KEY,
  sku          text           NOT NULL UNIQUE,
  name         text           NOT NULL,
  category     text           NOT NULL,
  price        numeric(10, 2) NOT NULL CHECK (price >= 0)
);

CREATE TABLE orders (
  order_id     serial PRIMARY KEY,
  customer_id  int            NOT NULL REFERENCES customers,
  product_id   int            NOT NULL REFERENCES products,
  quantity     int            NOT NULL CHECK (quantity > 0),
  amount       numeric(10, 2) NOT NULL,
  status       text           NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'paid', 'shipped', 'void')),
  ordered_at   timestamptz    NOT NULL DEFAULT now()
);

INSERT INTO customers (full_name, email, city) VALUES
  ('Asha Rao',      'asha.rao@example.com',      'Bengaluru'),
  ('Ravi Kumar',    'ravi.kumar@example.com',    'Hyderabad'),
  ('Meera Iyer',    'meera.iyer@example.com',    'Chennai'),
  ('Arjun Mehta',   'arjun.mehta@example.com',   'Mumbai'),
  ('Sara Thomas',   'sara.thomas@example.com',   'Kochi'),
  ('Vikram Singh',  'vikram.singh@example.com',  'Delhi'),
  ('Neha Gupta',    'neha.gupta@example.com',    'Pune'),
  ('Kiran Reddy',   'kiran.reddy@example.com',   'Hyderabad');

INSERT INTO products (sku, name, category, price) VALUES
  ('SKU-1001', 'Wireless Mouse',       'Accessories',  799.00),
  ('SKU-1002', 'Mechanical Keyboard',  'Accessories', 3499.00),
  ('SKU-1003', '27" Monitor',          'Displays',   14999.00),
  ('SKU-1004', 'USB-C Hub',            'Accessories', 1999.00),
  ('SKU-1005', 'Noise-Cancelling Headphones', 'Audio', 8999.00),
  ('SKU-1006', 'Laptop Stand',         'Accessories', 1499.00);

INSERT INTO orders (customer_id, product_id, quantity, amount, status, ordered_at)
SELECT c, p, q, q * (SELECT price FROM products WHERE product_id = p), s, now() - (n || ' hours')::interval
FROM (VALUES
  (1, 1, 2, 'paid', 70), (2, 3, 1, 'shipped', 64), (3, 2, 1, 'paid', 50), (4, 5, 1, 'new', 41),
  (5, 4, 3, 'paid', 30), (6, 6, 2, 'void', 26), (7, 1, 1, 'shipped', 20), (8, 3, 2, 'paid', 12),
  (1, 5, 1, 'new', 6), (3, 4, 1, 'paid', 2)
) AS v(c, p, q, s, n);

-- Debezium (pgoutput) streams these tables through this publication.
CREATE PUBLICATION dbz_publication FOR TABLE customers, products, orders;

-- Empty database a JDBC sink pipeline writes into.
CREATE DATABASE warehouse;
