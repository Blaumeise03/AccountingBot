import logging
from datetime import datetime
from time import sleep
from typing import Union, Optional, Tuple, List, Sequence

import mariadb
from mariadb import Cursor, Connection

from accounting_bot import utils
from accounting_bot.exceptions import DatabaseException

logger = logging.getLogger("ext.accounting.db")


class AccountingDB:
    def __init__(self, username: str, password: str, host: str, port: str, database: str) -> None:
        self.cursor = None  # type: Cursor | None
        self.con = None  # type: Connection | None
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.database = database
        connected = False
        counter = 0
        while not connected and counter < 5:
            # Retrying the connection in case the database is not yet ready
            try:
                self.try_connect()
                connected = True
            except mariadb.Error:
                counter += 1
                logger.warning(f"Retrying connection in {counter * 2} seconds")
                sleep(counter * 2)
        if not connected:
            raise DatabaseException(f"Couldn't connect to MariaDB database on {self.host}:{self.port}")

    def try_connect(self) -> None:
        logger.info("Connecting to database...")
        try:
            self.con = mariadb.connect(
                user=self.username,
                password=self.password,
                host=self.host,
                port=self.port,
                database=self.database,
                connect_timeout=8
            )
            logger.info("Connected to database!")
            self.cursor = self.con.cursor()
            self.cursor.execute("CREATE TABLE IF NOT EXISTS messages ("
                                "msgID BIGINT NOT NULL, "
                                "userID BIGINT NOT NULL, "
                                "verified BIT NOT NULL DEFAULT b'0', "
                                "t_state TINYINT, "
                                "ocr_verified BIT NOT NULL DEFAULT b'0', "
                                "PRIMARY KEY (msgID)"
                                ") ENGINE = InnoDB; ")
            self.cursor.execute("CREATE TABLE IF NOT EXISTS shortcuts ("
                                "msgID BIGINT NOT NULL, "
                                "channelID BIGINT NOT NULL, "
                                "PRIMARY KEY (msgID)"
                                ") ENGINE = InnoDB; ")
        except mariadb.Error as e:
            logger.error(f"Error connecting to MariaDB Platform: {e}")
            raise e

    def ping(self):
        try:
            if self.con is None or not self.con.open:
                self.try_connect()
            start = datetime.now()
            self.con.ping()
            return (datetime.now() - start).microseconds
        except mariadb.Error as e:
            utils.log_error(logger, e)
            return None

    def execute_statement(self, statement: str, data: Sequence = ()) -> Cursor:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(statement, data)
            self.con.commit()
            return self.cursor
        except mariadb.Error as e:
            logger.error("Error while trying to execute statement %s: %s", statement, e)
            raise e

    def add_transaction(self, message: int, user: int) -> None:
        logger.debug(f"Saving transaction to database with msg {str(message)} and user {str(user)}")
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

    def set_state(self, message: int, state: int) -> None:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "UPDATE messages SET t_state = ? WHERE messages.msgID=?;",
                (state, message))
            self.con.commit()
            return self.cursor.rowcount
        except mariadb.Error as e:
            logger.error(f"Error while trying to update the transaction {message} to state {state}: {e}")
            raise e

    def get_state(self, message: int) -> Optional[bool]:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "SELECT msgID, t_state FROM messages WHERE messages.msgID=?;",
                (message,))
            self.con.commit()
            res = self.cursor.fetchone()
            if res is None:
                return None
            (msgID, state) = res
            return state
        except mariadb.Error as e:
            logger.error(f"Error while trying to get state of a transaction: {e}")
            raise e

    def get_owner(self, message: int) -> Optional[Tuple[int, bool]]:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "SELECT userID, verified FROM messages WHERE msgID=?;",
                (message,))
            res = self.cursor.fetchone()
            if res is None:
                return None
            (user, verified) = res
            verified = verified == 1
            return user, verified
        except mariadb.Error as e:
            logger.error(f"Error while trying to get a transaction: {e}")
            raise e

    def set_verification(self, message: int, verified: Union[bool, int]) -> int:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "UPDATE messages SET verified = ? WHERE messages.msgID=?;",
                (verified, message))
            self.con.commit()
            return self.cursor.rowcount
        except mariadb.Error as e:
            logger.error(f"Error while trying to update the transaction {message} to {verified}: {e}")
            raise e

    def is_unverified_transaction(self, message: int) -> Optional[bool]:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "SELECT msgID, verified FROM messages WHERE messages.msgID=?;",
                (message,))
            self.con.commit()
            res = self.cursor.fetchone()
            if res is None:
                return None
            (msgID, verified) = res
            return verified == b'\x00'
        except mariadb.Error as e:
            logger.error(f"Error while trying to check a transaction: {e}")
            raise e

    def get_unverified(self, include_user: bool = False) -> Union[List[int], List[Tuple[int, int]]]:
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
            raise e

    def set_ocr_verification(self, message: int, verified: Union[bool, int]) -> int:
        if type(verified) == bool:
            verified = 1 if verified else 0
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "UPDATE messages SET ocr_verified = ? WHERE messages.msgID=?;",
                (verified, message))
            self.con.commit()
            return self.cursor.rowcount
        except mariadb.Error as e:
            logger.error(f"Error while trying to update the transaction {message} to ocr_verified {verified}: {e}")
            raise e

    def get_ocr_verification(self, message: int) -> Optional[bool]:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "SELECT msgID, ocr_verified FROM messages WHERE messages.msgID=?;",
                (message,))
            self.con.commit()
            res = self.cursor.fetchone()
            if res is None:
                return None
            (msgID, verified) = res
            return verified == b'\x01'
        except mariadb.Error as e:
            logger.error(f"Error while trying to check a transaction: {e}")
            raise e

    def delete(self, message: int) -> None:
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
                # logger.info(f"Deleted message {message}, affected {affected} rows")
                pass
        except mariadb.Error as e:
            logger.error(f"Error while trying to delete a transaction: {e}")
            raise e

    def add_shortcut(self, msg_id: int, channel_id: int) -> None:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "INSERT INTO shortcuts (msgID, channelID) VALUES (?, ?);",
                (msg_id, channel_id))
            self.con.commit()
            affected = self.cursor.rowcount
            if not affected == 1:
                logger.warning(f"Insertion of shortcut message {msg_id} affected {affected} rows, expected was 1 row")
            else:
                logger.info(f"Inserted shortcut message {msg_id}, affected {affected} rows")
        except mariadb.Error as e:
            logger.error(f"Error while trying to insert a shortcut message {msg_id}: {e}")
            raise e

    def get_shortcuts(self) -> List[Tuple[int, int]]:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            res = []
            self.cursor.execute(
                "SELECT msgID, channelID FROM shortcuts;")
            for (msg, channel) in self.cursor:
                res.append((msg, channel))
            return res
        except mariadb.Error as e:
            logger.error(f"Error while trying to get all shortcut messages: {e}")
            raise e

    def delete_shortcut(self, message: int) -> None:
        if self.con is None or not self.con.open:
            self.try_connect()
        try:
            self.cursor.execute(
                "DELETE FROM shortcuts WHERE shortcuts.msgID=?",
                (message,))
            self.con.commit()
            affected = self.cursor.rowcount
            if not affected == 1:
                logger.warning(f"Deletion of shortcut message {message} affected {affected} rows, expected was 1 row")
            else:
                logger.info(f"Deleted shortcut message {message}, affected {affected} rows")
        except mariadb.Error as e:
            logger.error(f"Error while trying to delete a shortcut message: {e}")
            raise e
