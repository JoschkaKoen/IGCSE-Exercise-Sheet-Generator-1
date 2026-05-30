-- Two-table relational dataset for the A-Level lessons (joins, DDL, DML).
-- Note: Ben and Dan have NO orders — useful for showing what INNER JOIN drops.
CREATE TABLE customer (
  id    INTEGER PRIMARY KEY,
  name  VARCHAR(20),
  city  VARCHAR(15)
);
INSERT INTO customer VALUES
  (1, 'Ada',  'London'),
  (2, 'Ben',  'Leeds'),
  (3, 'Cara', 'London'),
  (4, 'Dan',  'Hull');

CREATE TABLE orders (
  id           INTEGER PRIMARY KEY,
  customer_id  INTEGER,
  order_date   DATE,
  total        DECIMAL(7,2),
  FOREIGN KEY (customer_id) REFERENCES customer(id)
);
INSERT INTO orders VALUES
  (10, 1, '2024-02-01', 25.00),
  (11, 1, '2024-03-05',  9.50),
  (12, 3, '2024-03-06', 40.00),
  (13, 3, '2024-04-02', 12.00);

CREATE TABLE product (
  code      CHAR(3) PRIMARY KEY,
  title     VARCHAR(20),
  price     DECIMAL(6,2),
  in_stock  INTEGER
);
INSERT INTO product VALUES
  ('P01', 'Notebook', 2.50,  40),
  ('P02', 'Pen',      0.80, 200),
  ('P03', 'Backpack', 18.00, 15);
