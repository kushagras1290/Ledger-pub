import itertools
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tests.conftest import Api


def test_no_overdraft_under_contention(api: Api, cash: dict[str, Any]) -> None:
    source = api.account("wallet:source")
    sink = api.account("wallet:sink")
    api.fund(cash["id"], source["id"], 200)

    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(
            pool.map(lambda _: api.transfer(source["id"], sink["id"], 10).status_code, range(40))
        )

    assert statuses.count(201) == 20
    assert statuses.count(409) == 20
    assert api.balance(source["id"]) == 0
    assert api.balance(sink["id"]) == 200
    assert api.integrity()["ok"] is True


def test_opposing_transfers_do_not_deadlock(api: Api, cash: dict[str, Any]) -> None:
    wallets = [api.account(f"wallet:{i}") for i in range(4)]
    for w in wallets:
        api.fund(cash["id"], w["id"], 10_000)

    pairs = list(itertools.permutations([w["id"] for w in wallets], 2)) * 8

    with ThreadPoolExecutor(max_workers=24) as pool:
        statuses = list(pool.map(lambda p: api.transfer(p[0], p[1], 7).status_code, pairs))

    assert set(statuses) == {201}
    # Every wallet sent and received the same number of transfers.
    assert all(api.balance(w["id"]) == 10_000 for w in wallets)
    assert api.integrity()["ok"] is True
