"""``filter_dim_sources()`` 与 ``/filter/options`` 的 ``dim_sources``。

这张表送到客户端是为了讲一件后端一直在做、界面从没说过的事：一个过滤维度只
对登记了它的平台生效，其余平台整条跳过。断言因此分两类——

1. 它必须是 ``_SOURCE_FILTER_DIMS`` 的**转置**，不是另抄的一份；
2. 它必须和 ``source_supports_dim()`` 的实际判定逐格一致——转置写对了但和
   真正生效的那个函数对不上，客户端的提示语就是错的，而且不会有人发现。
"""
import config


def _all_dims() -> set[str]:
    dims = set()
    for caps in config._SOURCE_FILTER_DIMS.values():
        dims |= set(caps)
    return dims


class TestTranspose:
    def test_is_exact_transpose_of_source_table(self):
        got = config.filter_dim_sources()
        expected: dict[str, set[str]] = {}
        for source, caps in config._SOURCE_FILTER_DIMS.items():
            for dim in caps:
                expected.setdefault(dim, set()).add(source)
        assert {k: set(v) for k, v in got.items()} == expected

    def test_agrees_with_source_supports_dim_cell_by_cell(self):
        """逐格核对，而不是只看形状。"""
        table = config.filter_dim_sources()
        for source in config._SOURCE_FILTER_DIMS:
            for dim in _all_dims():
                assert config.source_supports_dim(source, dim) is (
                    source in table.get(dim, [])
                ), f"{source}/{dim}"

    def test_sources_are_sorted(self):
        table = config.filter_dim_sources()
        for dim, sources in table.items():
            assert sources == sorted(sources), dim

    def test_unregistered_source_appears_in_no_dim(self):
        """未登记平台走 _UNIVERSAL_FILTER_DIMS 回退，不该出现在表里。"""
        table = config.filter_dim_sources()
        for sources in table.values():
            assert "totally_new_site" not in sources


class TestKnownAsymmetries:
    """这些不对称正是要告诉用户的东西——它们变了，界面上的说明也得跟着改。"""

    def test_contract_neighborhood_offer_are_holland2stay_only(self):
        table = config.filter_dim_sources()
        for dim in ("contract", "neighborhood", "offer"):
            assert table[dim] == ["holland2stay"], dim

    def test_universal_dims_cover_every_registered_source(self):
        table = config.filter_dim_sources()
        every = sorted(config._SOURCE_FILTER_DIMS)
        for dim in config._UNIVERSAL_FILTER_DIMS:
            assert table[dim] == every, dim

    def test_partial_dims_are_neither_empty_nor_universal(self):
        """energy / floor / occupancy / finishing / type / tenant 都是"部分平台"。

        任何一个变成全覆盖或空，客户端的提示语就成了废话或谎话。
        """
        table = config.filter_dim_sources()
        every = set(config._SOURCE_FILTER_DIMS)
        for dim in ("energy", "floor", "occupancy", "finishing", "type", "tenant"):
            got = set(table[dim])
            assert got, dim
            assert got != every, f"{dim} 已全覆盖，PlatformScope 的提示该删了"
