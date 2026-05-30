-- Normalised (3NF) version of a sports-club booking, for the normalisation lesson.
-- An un-normalised single table would repeat the coach on every booking of a club
-- (the coach depends on the club, not on the booking — a transitive dependency).
-- Splitting club out into its own table stores each coach exactly once.
CREATE TABLE club (
  club_id  INTEGER PRIMARY KEY,
  name     VARCHAR(15),
  coach    VARCHAR(20)
);
INSERT INTO club VALUES
  (1, 'Chess',    'Mr Ng'),
  (2, 'Drama',    'Ms Patel'),
  (3, 'Robotics', 'Dr Sato');

CREATE TABLE booking (
  booking_id  INTEGER PRIMARY KEY,
  student     VARCHAR(20),
  club_id     INTEGER,
  FOREIGN KEY (club_id) REFERENCES club(club_id)
);
INSERT INTO booking VALUES
  (1, 'Mei',  1),
  (2, 'Tom',  1),
  (3, 'Sara', 2),
  (4, 'Lin',  3);
