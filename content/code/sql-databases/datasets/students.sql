-- Single-table dataset for the IGCSE-level reading lessons (Part A).
CREATE TABLE student (
  id     INTEGER PRIMARY KEY,
  name   VARCHAR(20),
  form   CHAR(3),
  score  INTEGER
);
INSERT INTO student VALUES
  (1, 'Mei',   '11A', 88),
  (2, 'Jamal', '11B', 72),
  (3, 'Sara',  '11A', 95),
  (4, 'Tom',   '11B', 64),
  (5, 'Lin',   '11A', 88);
