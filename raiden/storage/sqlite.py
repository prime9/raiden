import sqlite3
import threading
from contextlib import contextmanager

from raiden.constants import SQLITE_MIN_REQUIRED_VERSION
from raiden.exceptions import InvalidDBData, InvalidNumberInput
from raiden.storage.utils import DB_SCRIPT_CREATE_TABLES, TimestampedEvent
from raiden.utils import get_system_spec
from raiden.utils.typing import Any, Dict, NamedTuple, Optional, Tuple

from .serialize import SerializationBase

# The latest DB version
RAIDEN_DB_VERSION = 18


class EventRecord(NamedTuple):
    event_identifier: int
    state_change_identifier: int
    data: Any


class StateChangeRecord(NamedTuple):
    state_change_identifier: int
    data: Any


class SnapshotRecord(NamedTuple):
    identifier: int
    state_change_identifier: int
    data: Any


def assert_sqlite_version() -> bool:
    if sqlite3.sqlite_version_info < SQLITE_MIN_REQUIRED_VERSION:
        return False
    return True


class SQLiteStorage(SerializationBase):
    def __init__(self, database_path):
        conn = sqlite3.connect(database_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.text_factory = str
        conn.execute('PRAGMA foreign_keys=ON')

        # Skip the acquire/release cycle for the exclusive write lock.
        # References:
        # https://sqlite.org/atomiccommit.html#_exclusive_access_mode
        # https://sqlite.org/pragma.html#pragma_locking_mode
        conn.execute('PRAGMA locking_mode=EXCLUSIVE')

        # Keep the journal around and skip inode updates.
        # References:
        # https://sqlite.org/atomiccommit.html#_persistent_rollback_journals
        # https://sqlite.org/pragma.html#pragma_journal_mode
        try:
            conn.execute('PRAGMA journal_mode=PERSIST')
        except sqlite3.DatabaseError:
            raise InvalidDBData(
                f'Existing DB {database_path} was found to be corrupt at Raiden startup. '
                f'Manual user intervention required. Bailing.',
            )

        with conn:
            conn.executescript(DB_SCRIPT_CREATE_TABLES)

        # When writting to a table where the primary key is the identifier and we want
        # to return said identifier we use cursor.lastrowid, which uses sqlite's last_insert_rowid
        # https://github.com/python/cpython/blob/2.7/Modules/_sqlite/cursor.c#L727-L732
        #
        # According to the documentation (http://www.sqlite.org/c3ref/last_insert_rowid.html)
        # if a different thread tries to use the same connection to write into the table
        # while we query the last_insert_rowid, the result is unpredictable. For that reason
        # we have this write lock here.
        #
        # TODO (If possible):
        # Improve on this and find a better way to protect against this potential race
        # condition.
        self.conn = conn
        self.write_lock = threading.Lock()
        self.in_transaction = False
        self.update_version()

    def update_version(self):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
            ('version', str(RAIDEN_DB_VERSION)),
        )
        self.maybe_commit()

    def log_run(self):
        """ Log timestamp and raiden version to help with debugging """
        version = get_system_spec()['raiden']
        cursor = self.conn.cursor()
        cursor.execute('INSERT INTO runs(raiden_version) VALUES (?)', [version])
        self.maybe_commit()

    def get_version(self) -> int:
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM settings WHERE name=?;', ('version',),
        )
        query = query.fetchall()
        # If setting is not set, it's the latest version
        if len(query) == 0:
            return RAIDEN_DB_VERSION

        return int(query[0][0])

    def count_state_changes(self) -> int:
        cursor = self.conn.cursor()
        query = cursor.execute('SELECT COUNT(1) FROM state_changes')
        query = query.fetchall()

        if len(query) == 0:
            return 0

        return int(query[0][0])

    def write_state_change(self, state_change, log_time):
        with self.write_lock, self.conn:
            cursor = self.conn.execute(
                'INSERT INTO state_changes(identifier, data, log_time) VALUES(null, ?, ?)',
                (state_change, log_time),
            )
            last_id = cursor.lastrowid

        return last_id

    def write_state_snapshot(self, statechange_id, snapshot):
        with self.write_lock, self.conn:
            cursor = self.conn.execute(
                'INSERT INTO state_snapshot(statechange_id, data) VALUES(?, ?)',
                (statechange_id, snapshot),
            )
            last_id = cursor.lastrowid

        return last_id

    def write_events(self, state_change_identifier, events, log_time):
        """ Save events.

        Args:
            state_change_identifier: Id of the state change that generate these events.
            events: List of Event objects.
        """
        with self.write_lock, self.conn:
            self.conn.executemany(
                'INSERT INTO state_events('
                '   identifier, source_statechange_id, log_time, data'
                ') VALUES(?, ?, ?, ?)',
                events,
            )

    def get_latest_state_snapshot(self) -> Optional[Tuple[int, Any]]:
        """ Return the tuple of (last_applied_state_change_id, snapshot) or None"""
        cursor = self.conn.execute(
            'SELECT statechange_id, data from state_snapshot ORDER BY identifier DESC LIMIT 1',
        )
        rows = cursor.fetchall()

        if rows:
            assert len(rows) == 1
            last_applied_state_change_id = rows[0][0]
            snapshot_state = rows[0][1]
            return (last_applied_state_change_id, snapshot_state)

        return None

    def get_snapshot_closest_to_state_change(
            self,
            state_change_identifier: int,
    ) -> Tuple[int, Any]:
        """ Get snapshots earlier than state_change with provided ID. """

        if not (state_change_identifier == 'latest' or isinstance(state_change_identifier, int)):
            raise ValueError("from_identifier must be an integer or 'latest'")

        cursor = self.conn.cursor()
        if state_change_identifier == 'latest':
            cursor.execute(
                'SELECT identifier FROM state_changes ORDER BY identifier DESC LIMIT 1',
            )
            result = cursor.fetchone()

            if result:
                state_change_identifier = result[0]
            else:
                state_change_identifier = 0

        cursor = self.conn.execute(
            'SELECT statechange_id, data FROM state_snapshot '
            'WHERE statechange_id <= ? '
            'ORDER BY identifier DESC LIMIT 1',
            (state_change_identifier, ),
        )
        rows = cursor.fetchall()

        if rows:
            assert len(rows) == 1, 'LIMIT 1 must return one element'
            last_applied_state_change_id = rows[0][0]
            snapshot_state = rows[0][1]
            result = (last_applied_state_change_id, snapshot_state)
        else:
            result = (0, None)

        return result

    def get_latest_event_by_data_field(
            self,
            filters: Dict[str, Any],
    ) -> EventRecord:
        """ Return all state changes filtered by a named field and value."""
        cursor = self.conn.cursor()

        where_clauses = []
        args = []
        for field, value in filters.items():
            where_clauses.append('json_extract(data, ?)=?')
            args.append(f'$.{field}')
            args.append(value)

        cursor.execute(
            "SELECT identifier, source_statechange_id, data FROM state_events WHERE "
            f"{' AND '.join(where_clauses)}"
            "ORDER BY identifier DESC LIMIT 1",
            args,
        )

        result = EventRecord(
            event_identifier=0,
            state_change_identifier=0,
            data=None,
        )
        try:
            row = cursor.fetchone()
            if row:
                event_id = row[0]
                state_change_identifier = row[1]
                event = row[2]
                result = EventRecord(
                    event_identifier=event_id,
                    state_change_identifier=state_change_identifier,
                    data=event,
                )
        except AttributeError:
            raise InvalidDBData(
                'Your local database is corrupt. Bailing ...',
            )

        return result

    def get_latest_state_change_by_data_field(
            self,
            filters: Dict[str, str],
    ) -> StateChangeRecord:
        """ Return all state changes filtered by a named field and value."""
        cursor = self.conn.cursor()

        where_clauses = []
        args = []
        for field, value in filters.items():
            where_clauses.append('json_extract(data, ?)=?')
            args.append(f'$.{field}')
            args.append(value)

        where = ' AND '.join(where_clauses)
        sql = (
            f'SELECT identifier, data '
            f'FROM state_changes '
            f'WHERE {where} '
            f'ORDER BY identifier '
            f'DESC LIMIT 1'
        )
        cursor.execute(sql, args)

        result = StateChangeRecord(state_change_identifier=0, data=None)
        try:
            row = cursor.fetchone()
            if row:
                state_change_identifier = row[0]
                state_change = row[1]
                result = StateChangeRecord(
                    state_change_identifier=state_change_identifier,
                    data=state_change,
                )
        except AttributeError:
            raise InvalidDBData(
                'Your local database is corrupt. Bailing ...',
            )

        return result

    def get_statechanges_by_identifier(self, from_identifier, to_identifier):
        if not (from_identifier == 'latest' or isinstance(from_identifier, int)):
            raise ValueError("from_identifier must be an integer or 'latest'")

        if not (to_identifier == 'latest' or isinstance(to_identifier, int)):
            raise ValueError("to_identifier must be an integer or 'latest'")

        cursor = self.conn.cursor()

        if from_identifier == 'latest':
            assert to_identifier is None

            cursor.execute(
                'SELECT identifier FROM state_changes ORDER BY identifier DESC LIMIT 1',
            )
            from_identifier = cursor.fetchone()

        if to_identifier == 'latest':
            cursor.execute(
                'SELECT data FROM state_changes WHERE identifier >= ? ORDER BY identifier ASC',
                (from_identifier,),
            )
        else:
            cursor.execute(
                'SELECT data FROM state_changes WHERE identifier '
                'BETWEEN ? AND ? ORDER BY identifier ASC', (from_identifier, to_identifier),
            )

        try:
            result = [entry[0] for entry in cursor.fetchall()]
        except AttributeError:
            raise InvalidDBData(
                'Your local database is corrupt. Bailing ...',
            )

        return result

    def _query_events(self, limit: int = None, offset: int = None):
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise InvalidNumberInput('limit must be a positive integer')

        if offset is not None and (not isinstance(offset, int) or offset < 0):
            raise InvalidNumberInput('offset must be a positive integer')

        limit = -1 if limit is None else limit
        offset = 0 if offset is None else offset

        cursor = self.conn.cursor()

        cursor.execute(
            '''
            SELECT data, log_time FROM state_events
                ORDER BY identifier ASC LIMIT ? OFFSET ?
            ''',
            (limit, offset),
        )

        return cursor.fetchall()

    def get_events_with_timestamps(self, limit: int = None, offset: int = None):
        entries = self._query_events(limit, offset)
        return [
            TimestampedEvent(entry[0], entry[1])
            for entry in entries
        ]

    def get_events(self, limit: int = None, offset: int = None):
        entries = self._query_events(limit, offset)
        return [entry[0] for entry in entries]

    def get_snapshots(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT identifier, statechange_id, data FROM state_snapshot')
        snapshots = cursor.fetchall()

        return [
            SnapshotRecord(snapshot[0], snapshot[1], snapshot[2])
            for snapshot in snapshots
        ]

    def update_snapshot(self, identifier, new_snapshot):
        cursor = self.conn.cursor()
        cursor.execute(
            'UPDATE state_snapshot SET data=? WHERE identifier=?',
            (new_snapshot, identifier),
        )
        self.maybe_commit()

    def maybe_commit(self):
        if not self.in_transaction:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        cursor = self.conn.cursor()
        self.in_transaction = True
        try:
            cursor.execute('BEGIN')
            yield
            cursor.execute('COMMIT')
        except Exception:
            cursor.execute('ROLLBACK')
            raise
        finally:
            self.in_transaction = False

    def __del__(self):
        self.conn.close()


class SerializedSQLiteStorage(SQLiteStorage):
    def __init__(self, database_path, serializer: SerializationBase):
        super().__init__(database_path)

        self.serializer = serializer

    def write_state_change(self, state_change, log_time):
        serialized_data = self.serializer.serialize(state_change)
        return super().write_state_change(serialized_data, log_time)

    def write_state_snapshot(self, statechange_id, snapshot):
        serialized_data = self.serializer.serialize(snapshot)
        return super().write_state_snapshot(statechange_id, serialized_data)

    def write_events(self, state_change_identifier, events, log_time):
        """ Save events.

        Args:
            state_change_identifier: Id of the state change that generate these events.
            events: List of Event objects.
        """
        events_data = [
            (None, state_change_identifier, log_time, self.serializer.serialize(event))
            for event in events
        ]
        return super().write_events(state_change_identifier, events_data, log_time)

    def get_latest_state_snapshot(self) -> Optional[Tuple[int, Any]]:
        """ Return the tuple of (last_applied_state_change_id, snapshot) or None"""
        row = super().get_latest_state_snapshot()

        if row:
            last_applied_state_change_id = row[0]
            snapshot_state = self.serializer.deserialize(row[1])
            return (last_applied_state_change_id, snapshot_state)

        return None

    def get_snapshot_closest_to_state_change(
            self,
            state_change_identifier: int,
    ) -> Tuple[int, Any]:
        """ Get snapshots earlier than state_change with provided ID. """

        row = super().get_snapshot_closest_to_state_change(state_change_identifier)

        if row[1]:
            last_applied_state_change_id = row[0]
            snapshot_state = self.serializer.deserialize(row[1])
            result = (last_applied_state_change_id, snapshot_state)
        else:
            result = (0, None)

        return result

    def get_latest_event_by_data_field(
            self,
            filters: Dict[str, Any],
    ) -> EventRecord:
        """ Return all state changes filtered by a named field and value."""
        event = super().get_latest_event_by_data_field(filters)

        if event.event_identifier > 0:
            event = EventRecord(
                event_identifier=event.event_identifier,
                state_change_identifier=event.state_change_identifier,
                data=self.serializer.deserialize(event.data),
            )

        return event

    def get_latest_state_change_by_data_field(
            self,
            filters: Dict[str, str],
    ) -> StateChangeRecord:
        """ Return all state changes filtered by a named field and value."""

        state_change = super().get_latest_state_change_by_data_field(filters)

        if state_change.state_change_identifier > 0:
            state_change = StateChangeRecord(
                state_change_identifier=state_change.state_change_identifier,
                data=self.serializer.deserialize(state_change.data),
            )

        return state_change

    def get_statechanges_by_identifier(self, from_identifier, to_identifier):
        state_changes = super().get_statechanges_by_identifier(from_identifier, to_identifier)
        return [
            self.serializer.deserialize(state_change)
            for state_change in state_changes
        ]

    def get_events_with_timestamps(self, limit: int = None, offset: int = None):
        events = super().get_events_with_timestamps(limit, offset)
        return [
            TimestampedEvent(
                self.serializer.deserialize(event.wrapped_event),
                event.log_time,
            )
            for event in events
        ]

    def get_events(self, limit: int = None, offset: int = None):
        events = super().get_events(limit, offset)
        return [self.serializer.deserialize(event) for event in events]
