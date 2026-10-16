"""C-3.2/C-5.7: retry clocks commit without duplicating the state audit."""
import pytest

from subfleet.store import Store


def test_unaudited_bookkeeping_is_atomic_visible_and_increments_generation(tmp_path):
    with Store(tmp_path / 'state.sqlite3', readers=2) as store:
        before = len(store.query('SELECT * FROM events'))
        generation = store.generation
        with store.transaction('clock', audit=False) as tx:
            tx.execute("INSERT INTO leases VALUES ('fixture-clock','fixture','t',NULL)")
        assert store.one("SELECT holder FROM leases WHERE lease_key='fixture-clock'")['holder'] == 'fixture'
        assert store.generation == generation + 1
        assert len(store.query('SELECT * FROM events')) == before
        with pytest.raises(RuntimeError):
            with store.transaction('clock', audit=False) as tx:
                tx.execute("UPDATE leases SET holder='rolled-back' WHERE lease_key='fixture-clock'")
                raise RuntimeError('interrupt')
        assert store.one("SELECT holder FROM leases WHERE lease_key='fixture-clock'")['holder'] == 'fixture'
        assert store.generation == generation + 1
        with store.transaction('fixture-transition') as tx:
            tx.execute("UPDATE leases SET holder='new-holder' WHERE lease_key='fixture-clock'")
        assert store.one("SELECT kind FROM events WHERE kind='fixture-transition'")
        assert len(store.query('SELECT * FROM events')) == before + 1
