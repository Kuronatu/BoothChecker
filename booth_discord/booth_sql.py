import functools
import threading
import time
from contextlib import contextmanager

import psycopg
from psycopg import errors as pg_errors


def _synchronized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class BoothPostgres:
    def __init__(self, conn_params, booth, logger):
        self.logger = logger
        self.booth = booth
        self._lock = threading.RLock()
        self.conn = self._connect_with_retry(conn_params)
        self.conn.autocommit = True

        with self.conn.transaction():
            with self.conn.cursor() as cursor:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS booth_accounts (
                        session_cookie TEXT UNIQUE,
                        discord_user_id BIGINT PRIMARY KEY
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS booth_items (
                        booth_order_number TEXT PRIMARY KEY,
                        booth_item_number TEXT,
                        discord_user_id BIGINT,
                        item_name TEXT,
                        intent_encoding TEXT,
                        download_number_show BOOLEAN,
                        changelog_show BOOLEAN,
                        archive_this BOOLEAN,
                        gift_item BOOLEAN,
                        summary_this BOOLEAN,
                        fbx_only BOOLEAN,
                        FOREIGN KEY(discord_user_id) REFERENCES booth_accounts(discord_user_id)
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS discord_noti_channels (
                        discord_channel_id BIGINT,
                        booth_order_number TEXT,
                        UNIQUE(discord_channel_id, booth_order_number),
                        FOREIGN KEY(booth_order_number) REFERENCES booth_items(booth_order_number)
                    )
                ''')

    def __del__(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _connect_with_retry(self, conn_params, retries=5, delay=2):
        for attempt in range(1, retries + 1):
            try:
                return psycopg.connect(**conn_params)
            except psycopg.OperationalError as exc:
                if attempt == retries:
                    self.logger.error("PostgreSQL 연결에 실패했습니다. 설정을 확인해주세요.")
                    raise
                self.logger.warning(
                    "PostgreSQL 연결 재시도 %s/%s: %s",
                    attempt,
                    retries,
                    exc,
                )
                time.sleep(delay)

    @contextmanager
    def _transaction(self):
        with self.conn.transaction():
            with self.conn.cursor() as cursor:
                yield cursor

    @_synchronized
    def add_booth_account(self, session_cookie, discord_user_id):
        with self.conn.cursor() as cursor:
            cursor.execute('''
                SELECT discord_user_id FROM booth_accounts
                WHERE session_cookie = %s
            ''', (session_cookie,))
            owner = cursor.fetchone()
        if owner and owner[0] != discord_user_id:
            raise Exception("이미 다른 Discord 계정에 등록된 쿠키입니다.")

        existing_account = self.get_booth_account(discord_user_id)
        with self._transaction() as cursor:
            if existing_account:
                cursor.execute('''
                    UPDATE booth_accounts
                    SET session_cookie = %s
                    WHERE discord_user_id = %s
                ''', (session_cookie, discord_user_id))
            else:
                cursor.execute('''
                    INSERT INTO booth_accounts (session_cookie, discord_user_id)
                    VALUES (%s, %s)
                ''', (session_cookie, discord_user_id))
        return self.get_booth_account(discord_user_id)

    @_synchronized
    def add_booth_item(self, discord_user_id, discord_channel_id, booth_item_number, booth_order_number, item_name, intent_encoding, summary_this, fbx_only):
        booth_account = self.get_booth_account(discord_user_id)
        if self.is_item_duplicate(booth_item_number, discord_user_id):
            raise Exception("이미 등록된 아이템입니다.")
        if not booth_account:
            raise Exception("BOOTH 계정이 등록되어 있지 않습니다.")

        if booth_order_number:
            booth_order_info = (False, booth_order_number)
        else:
            booth_order_info = self.booth.get_booth_order_info(booth_item_number, ("_plaza_session_nktz7u", booth_account[0]))
        
        try:
            with self._transaction() as cursor:
                cursor.execute('''
                    INSERT INTO booth_items (
                                    booth_order_number,
                                    booth_item_number,
                                    discord_user_id,
                                    item_name,
                                    intent_encoding,
                                    download_number_show,
                                    changelog_show,
                                    archive_this,
                                    gift_item,
                                    summary_this,
                                    fbx_only
                                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (booth_order_info[1],  # booth_order_number
                      booth_item_number,
                      discord_user_id,
                      item_name,
                      intent_encoding,
                      True,                 # download_number_show
                      True,                 # changelog_show
                      False,                # archive_this
                      booth_order_info[0],  # gift_item
                      summary_this,
                      fbx_only))
                self.add_discord_noti_channel(
                    discord_channel_id,
                    booth_order_info[1],
                    use_transaction=False,
                    cursor=cursor,
                )
        except pg_errors.IntegrityError as exc:
            raise Exception("아이템 등록 중 충돌이 발생했습니다.") from exc
        return booth_order_info[1]

    @_synchronized
    def del_booth_account(self, discord_user_id):
        try:
            with self.conn.cursor() as cursor:
                cursor.execute('''
                    SELECT EXISTS (
                        SELECT 1
                        FROM booth_items
                        WHERE discord_user_id = %s
                    );
                ''', (discord_user_id,))
                result = cursor.fetchone()
            if result[0] == 1:
                raise Exception("BOOTH 아이템이 등록되어 있습니다. 먼저 아이템을 삭제해주세요.")
            with self._transaction() as cursor:
                cursor.execute('''
                    DELETE FROM booth_accounts WHERE discord_user_id = %s
                ''', (discord_user_id,))
                return cursor.rowcount
        except Exception:
            raise

    @_synchronized
    def del_booth_item(self, discord_user_id, booth_item_number):
        booth_account = self.get_booth_account(discord_user_id)
        if not booth_account:
            raise Exception("BOOTH 계정이 등록되어 있지 않습니다.")

        try:
            with self.conn.cursor() as cursor:
                cursor.execute('''
                    SELECT booth_order_number FROM booth_items
                    WHERE booth_item_number = %s AND discord_user_id = %s
                ''', (booth_item_number, discord_user_id))
                result = cursor.fetchone()
            if not result:
                raise Exception(f"Item {booth_item_number} not found for user {discord_user_id}")

            booth_order_number = result[0]
            self.logger.debug("booth_order_number: %s", booth_order_number)

            with self._transaction() as cursor:
                deleted_channels = self.del_discord_noti_channel(
                    booth_order_number,
                    use_transaction=False,
                    cursor=cursor,
                )
                cursor.execute('''
                    DELETE FROM booth_items
                    WHERE booth_item_number = %s AND discord_user_id = %s
                ''', (booth_item_number, discord_user_id))
                deleted_items = cursor.rowcount
            return {'items_deleted': deleted_items, 'channels_deleted': deleted_channels}
        except Exception:
            raise

    @_synchronized
    def get_booth_account(self, discord_user_id):
        with self.conn.cursor() as cursor:
            cursor.execute('''
                SELECT * FROM booth_accounts
                WHERE discord_user_id = %s
            ''', (discord_user_id,))
            result = cursor.fetchone()
        return result if result else None

    @_synchronized
    def is_item_duplicate(self, booth_item_number, discord_user_id):
        with self.conn.cursor() as cursor:
            cursor.execute('''
                SELECT 1 FROM booth_items
                WHERE booth_item_number = %s AND discord_user_id = %s
            ''', (booth_item_number, discord_user_id))
            return cursor.fetchone() is not None

    @_synchronized
    def list_booth_items(self, discord_user_id, discord_channel_id):
        booth_account = self.get_booth_account(discord_user_id)
        if not booth_account:
            raise Exception("BOOTH 계정이 등록되어 있지 않습니다.")

        with self.conn.cursor() as cursor:
            cursor.execute('''
                SELECT bi.booth_item_number
                FROM booth_items bi
                JOIN discord_noti_channels dnc
                ON bi.booth_order_number = dnc.booth_order_number
                WHERE bi.discord_user_id = %s
                AND dnc.discord_channel_id = %s;
            ''', (discord_user_id, discord_channel_id))
            return cursor.fetchall()

    @_synchronized
    def add_discord_noti_channel(self, discord_channel_id, booth_order_number, use_transaction=True, cursor=None):
        if cursor is not None:
            return self._insert_discord_noti_channel(cursor, discord_channel_id, booth_order_number)
        if use_transaction:
            with self._transaction() as tx_cursor:
                return self._insert_discord_noti_channel(tx_cursor, discord_channel_id, booth_order_number)
        with self.conn.cursor() as standalone_cursor:
            return self._insert_discord_noti_channel(standalone_cursor, discord_channel_id, booth_order_number)

    @_synchronized
    def del_discord_noti_channel(self, booth_order_number, use_transaction=True, cursor=None):
        self.logger.debug("del_discord_noti_channel - booth_order_number : %s", booth_order_number)
        if cursor is not None:
            return self._delete_discord_noti_channel(cursor, booth_order_number)
        if use_transaction:
            with self._transaction() as tx_cursor:
                return self._delete_discord_noti_channel(tx_cursor, booth_order_number)
        with self.conn.cursor() as standalone_cursor:
            return self._delete_discord_noti_channel(standalone_cursor, booth_order_number)

    @_synchronized
    def update_discord_noti_channel(self, discord_user_id, discord_channel_id, booth_item_number):
        booth_order_number = self.get_booth_order_number(booth_item_number, discord_user_id)
        if not booth_order_number:
            raise Exception("Item not found")
        with self._transaction() as cursor:
            cursor.execute('''
                UPDATE discord_noti_channels
                SET discord_channel_id = %s
                WHERE booth_order_number = %s
            ''', (discord_channel_id, booth_order_number))
            return cursor.rowcount

    @_synchronized
    def get_booth_order_number(self, booth_item_number, discord_user_id):
        with self.conn.cursor() as cursor:
            cursor.execute('''
                SELECT booth_order_number FROM booth_items
                WHERE booth_item_number = %s AND discord_user_id = %s
            ''', (booth_item_number, discord_user_id))
            result = cursor.fetchone()
        return result[0] if result else None
    
    @_synchronized
    def get_booth_item_count(self, discord_user_id):
        with self.conn.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM booth_items WHERE discord_user_id = %s', (discord_user_id,))
            result = cursor.fetchone()
        return result[0] if result else 0

    def _insert_discord_noti_channel(self, cursor, discord_channel_id, booth_order_number):
        cursor.execute('''
            SELECT 1 FROM discord_noti_channels
            WHERE discord_channel_id = %s AND booth_order_number = %s
        ''', (discord_channel_id, booth_order_number))
        if cursor.fetchone():
            return False

        cursor.execute('''
            INSERT INTO discord_noti_channels (discord_channel_id, booth_order_number)
            VALUES (%s, %s)
        ''', (discord_channel_id, booth_order_number))
        return True

    def _delete_discord_noti_channel(self, cursor, booth_order_number):
        cursor.execute('''
            DELETE FROM discord_noti_channels WHERE booth_order_number = %s
        ''', (booth_order_number,))
        return cursor.rowcount
