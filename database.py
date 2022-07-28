import logging
from time import sleep

import mariadb

logger = logging.getLogger(__name__)


class DatabaseConnector:
    def __init__(self, username, password, host, port, database):
        self.cursor = None
        self.con = None
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.database = database
        connected = False
        counter = 0
        while not connected and counter < 5:
            try:
                self.try_connect()
                connected = True
            except mariadb.Error as e:
                counter += 1
                logger.warning(f"Retrying connection in {counter*2} seconds")
                sleep(counter*2)

    def try_connect(self):
        logging.info("Connecting to database...")
        try:
            self.con = mariadb.connect(
                user=self.username,
                password=self.password,
                host=self.host,
                port=self.port,
                database=self.database,
                connect_timeout=8
            )
            logging.info("Connected to database!")
            self.cursor = self.con.cursor()
            self.cursor.execute("CREATE TABLE IF NOT EXISTS messages ("
                                "msgID BIGINT NOT NULL, "
                                "userID BIGINT NOT NULL, "
                                "verified BIT NOT NULL DEFAULT b'0', "
                                "PRIMARY KEY (msgID)"
                                ") ENGINE = InnoDB; ")
        except mariadb.Error as e:
            logger.error(f"Error connecting to MariaDB Platform: {e}")
            raise e

    def add_transaction(self, message, user):
        logger.debug(f"Saving transaction to database with msg {message} and user {user}")
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "INSERT INTO messages (msgID, userID) VALUES (?, ?);",
                (message, user))
            self.con.commit()
        except mariadb.Error as e:
            logger.error(f"Error while trying to insert a new transaction: {e}")
            raise e

    def get_owner(self, message):
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "SELECT userID, verified FROM messages WHERE msgID=?;",
                (message,))
            (user, verified) = self.cursor.fetchone()
            verified = verified == 1
            return user, verified
        except mariadb.Error as e:
            logger.error(f"Error while trying to get a transaction: {e}")
            raise e

    def set_verification(self, message, verified):
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "UPDATE messages SET verified = ? WHERE messages.msgID=?;",
                (verified, message))
            self.con.commit()
            return self.cursor.rowcount
        except mariadb.Error as e:
            logger.error(f"Error while trying to insert a new transaction: {e}")
            raise e

    def is_unverified_transaction(self, message):
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "SELECT msgID, verified FROM messages WHERE messages.msgID=?;",
                (message, ))
            self.con.commit()
            res = self.cursor.fetchone()
            if res is None:
                return None
            (msgID, verified) = res
            return verified == b'\x00'
        except mariadb.Error as e:
            logger.error(f"Error while trying to check a transaction: {e}")
            return False

    def get_unverified(self, include_user=False):
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            res = []
            if include_user:
                self.cursor.execute(
                    "SELECT msgID, userID FROM messages WHERE verified=b'0';")
                for (msg, user) in self.cursor:
                    res.append((msg, user))
            else:
                self.cursor.execute(
                    "SELECT msgID FROM messages WHERE verified=b'0';")
                for (msg,) in self.cursor:
                    res.append(msg)
            return res
        except mariadb.Error as e:
            logger.error(f"Error while trying to get all unverified transactions: {e}")

    def delete(self, message):
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "DELETE FROM messages WHERE messages.msgID=?",
                (message,))
            self.con.commit()
            affected = self.cursor.rowcount
            if not affected == 1:
                logger.warning(f"Deletion of message {message} affected {affected} rows, expected was 1 row")
            else:
                logger.info(f"Deleted message {message}, affected {affected} rows")
        except mariadb.Error as e:
            logger.error(f"Error while trying to delete a transaction: {e}")
