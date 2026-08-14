# name -> (default TTL seconds — LIVEMAP_WB_*_CACHE_TTL_S-overridable — and fixed max size).
_CACHE_SPECS = {"airlines": (300.0, 64), "services": (300.0, 256), "instances": (120.0, 512),
                "search": (120.0, 256), "summary": (300.0, 64), "trends": (300.0, 128),
                "flags": (120.0, 256), "estimates": (300.0, 64), "coverage": (300.0, 64)}


# Workbench evidence layer (private-only): everything constructor-injected (no globals-backed
# ctx), and the store gets ONLY the read-client factory — never the writer seam.
class WorkbenchStore:
    def __init__(self, wb, cache_put, client_factory, is_unknown_table_error, env_num):
        self.wb = wb
        self.cache_put = cache_put
        self.client_factory = client_factory
        self._is_unknown_table_error = is_unknown_table_error
        self.caches = {name: {} for name in _CACHE_SPECS}
        self.max_sizes = {name: size for name, (_ttl, size) in _CACHE_SPECS.items()}
        self.ttls = {name: env_num(f"LIVEMAP_WB_{name.upper()}_CACHE_TTL_S", ttl)
                     for name, (ttl, _size) in _CACHE_SPECS.items()}

    def probe_tables(self, client) -> set:
        return {r[0] for r in client.query(self.wb.PROBE_TABLES_QUERY,
                                           parameters={"db": self.wb.CH_DB}).result_rows}

    # Thin fetchers: SQL text + row shaping live in wb; this layer only owns the CH round trip
    # and the tier-mart-absent degradation (query, and on unknown-table, requery).
    def fetch_airlines(self, q, limit, offset) -> dict:
        wb = self.wb
        params = {"q": (q or "").strip(), "limit": limit, "offset": offset}
        client = self.client_factory()
        try:
            try:
                rows = client.query(wb.AIRLINES_QUERY_TIER, parameters=params).result_rows
                with_tier = True
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                rows = client.query(wb.AIRLINES_QUERY_NO_TIER, parameters=params).result_rows
                with_tier = False
            total = client.query(wb.AIRLINES_COUNT_QUERY, parameters={"q": params["q"]}).result_rows[0][0]
        finally:
            client.close()
        return {"airlines": [wb.shape_airline_row(r, with_tier) for r in rows],
                "total": total, "limit": limit, "offset": offset}

    def fetch_services(self, airline, q, limit, offset) -> dict:
        wb = self.wb
        params = {"airline": (airline or "").strip(), "q": (q or "").strip().upper(),
                  "limit": limit, "offset": offset}
        client = self.client_factory()
        try:
            try:
                rows = client.query(wb.SERVICES_QUERY_TIER, parameters=params).result_rows
                with_tier = True
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                rows = client.query(wb.SERVICES_QUERY_NO_TIER, parameters=params).result_rows
                with_tier = False
            total = client.query(
                wb.SERVICES_COUNT_QUERY, parameters={"airline": params["airline"], "q": params["q"]}
            ).result_rows[0][0]
            callsigns = [r[0] for r in rows]
            top_od_rows = (
                client.query(wb.SERVICES_TOP_OD_QUERY, parameters={"callsigns": callsigns}).result_rows
                if callsigns else []
            )
        finally:
            client.close()
        top_od = wb.group_top_od(top_od_rows)
        return {"services": [wb.shape_service_row(r, with_tier, top_od) for r in rows],
                "total": total, "limit": limit, "offset": offset}

    def fetch_instances(self, callsign, airline, hex_, reg, airport, od, type_, military,
                        day_from, day_to, sort, limit, offset) -> dict:
        wb = self.wb
        params = wb.instances_params(callsign, airline, hex_, reg, airport, od, type_, military,
                                     day_from, day_to)
        params["limit"] = limit
        params["offset"] = offset
        asc = sort == "day_asc"
        client = self.client_factory()
        try:
            try:
                rows = client.query(wb.instances_query(tier=True, asc=asc), parameters=params,
                                    settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
                total = client.query(wb.instances_count_query(tier=True),
                                     parameters=params).result_rows[0][0]
                od_rows = client.query(wb.instances_od_breakdown_query(tier=True),
                                       parameters=params).result_rows
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                # military filtering has no meaning without the tier mart — an honest empty, not a silent no-op
                if params["military"]:
                    return {"instances": [], "od_breakdown": [], "total": 0, "limit": limit,
                            "offset": offset, "military_filter_available": False}
                rows = client.query(wb.instances_query(tier=False, asc=asc), parameters=params,
                                    settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
                total = client.query(wb.instances_count_query(tier=False),
                                     parameters=params).result_rows[0][0]
                od_rows = client.query(wb.instances_od_breakdown_query(tier=False),
                                       parameters=params).result_rows
        finally:
            client.close()
        return {"instances": [wb.shape_instance_row(r) for r in rows],
                "od_breakdown": wb.shape_od_breakdown(od_rows),
                "total": total, "limit": limit, "offset": offset}

    def fetch_summary(self, day_from, day_to) -> dict:
        wb = self.wb
        params = wb.summary_params(day_from, day_to)
        has_flags = has_tier = has_est = True
        client = self.client_factory()
        try:
            try:
                rows = client.query(wb.summary_query(True, True, True), parameters=params).result_rows
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                present = self.probe_tables(client)
                has_flags = "fct_flight_flags" in present
                has_tier = "fct_flight_recon_tier" in present
                has_est = "fct_est_settlement" in present
                rows = client.query(wb.summary_query(has_flags, has_tier, has_est),
                                    parameters=params).result_rows
        finally:
            client.close()
        return wb.shape_summary(rows, has_flags, has_tier, has_est)

    def fetch_trends(self, dim, day_from, day_to, limit, offset) -> dict:
        wb = self.wb
        # dim only ever selects among pre-built query texts — the wire value never reaches SQL
        dim = dim if dim in wb.TRENDS_RANK_QUERY else "route"
        params = wb.trends_params(day_from, day_to, limit, offset)
        client = self.client_factory()
        try:
            rank_rows = client.query(wb.TRENDS_RANK_QUERY[dim], parameters=params).result_rows
            keys = [r[0] for r in rank_rows]
            series_rows = (
                client.query(wb.TRENDS_SERIES_QUERY[dim],
                             parameters=params | {"keys": keys}).result_rows
                if keys else []
            )
            total = client.query(wb.TRENDS_TOTAL_QUERY[dim], parameters=params).result_rows[0][0]
        finally:
            client.close()
        return wb.shape_trends(dim, rank_rows, series_rows, total, limit, offset)

    def fetch_flags(self, class_, day_from, day_to, limit, offset) -> dict:
        wb = self.wb
        params = wb.flags_params(class_, day_from, day_to)
        params["limit"] = limit
        params["offset"] = offset
        client = self.client_factory()
        try:
            try:
                rows = client.query(wb.FLAGS_QUERY_TIER, parameters=params,
                                    settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                try:
                    rows = client.query(wb.FLAGS_QUERY_NO_TIER, parameters=params,
                                        settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
                except Exception as exc2:
                    if not self._is_unknown_table_error(exc2):
                        raise
                    # the flags mart itself is missing — say so, don't serve a plausible-looking empty feed
                    return {"available": False, "flags": [], "classes": {}, "total": 0,
                            "limit": limit, "offset": offset}
            total = client.query(wb.FLAGS_COUNT_QUERY, parameters=params).result_rows[0][0]
            class_rows = client.query(wb.FLAGS_CLASSES_QUERY, parameters=params).result_rows
        finally:
            client.close()
        return {"available": True, "flags": [wb.shape_flag_row(r) for r in rows],
                "classes": {c: n for c, n in class_rows},
                "total": total, "limit": limit, "offset": offset}

    def fetch_estimates(self, day_from, day_to) -> dict:
        wb = self.wb
        params = wb.estimates_params(day_from, day_to)
        mix_available = True
        client = self.client_factory()
        try:
            try:
                headline = client.query(wb.ESTIMATES_HEADLINE_QUERY, parameters=params).result_rows
                daily = client.query(wb.ESTIMATES_DAILY_QUERY, parameters=params).result_rows
                outcomes = client.query(wb.ESTIMATES_OUTCOMES_QUERY, parameters=params).result_rows
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                # only fct_est_settlement can be missing here — no section keeps a source without it
                return wb.empty_estimates() | {"available": False}
            try:
                mix = client.query(wb.ESTIMATES_MIX_QUERY, parameters=params).result_rows
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                # the breakdown mart deploys independently of the ledger — only the mix goes dark
                mix, mix_available = [], False
        finally:
            client.close()
        return wb.shape_estimates(headline, daily, mix, outcomes[0] if outcomes else None,
                                  mix_available)

    def fetch_coverage(self, day_from, day_to) -> dict:
        wb = self.wb
        params = wb.coverage_params(day_from, day_to)
        client = self.client_factory()
        try:
            try:
                tier_rows = client.query(wb.COVERAGE_TIER_DAILY_QUERY, parameters=params).result_rows
                gap_rows = client.query(wb.COVERAGE_GAP_HIST_QUERY, parameters=params).result_rows
                obs_rows = client.query(wb.COVERAGE_OBSERVED_QUERY, parameters=params).result_rows
            except Exception as exc:
                if not self._is_unknown_table_error(exc):
                    raise
                # the reconciled mart is never optional, so this can only be the tier mart
                return wb.empty_coverage() | {"available": False}
        finally:
            client.close()
        return wb.shape_coverage(tier_rows, gap_rows, obs_rows)

    def fetch_search(self, q, limit) -> dict:
        wb = self.wb
        params = wb.search_params(q)
        params["limit"] = limit
        client = self.client_factory()
        try:
            airlines = client.query(wb.SEARCH_AIRLINES_QUERY, parameters=params).result_rows
            services = client.query(wb.SEARCH_SERVICES_QUERY, parameters=params).result_rows
            airframes = client.query(wb.SEARCH_AIRFRAMES_QUERY, parameters=params).result_rows
            airports = client.query(wb.SEARCH_AIRPORTS_QUERY, parameters=params).result_rows
        finally:
            client.close()
        return {
            "airlines": [wb.shape_search_airline(r) for r in airlines],
            "services": [wb.shape_search_service(r) for r in services],
            "airframes": [wb.shape_search_airframe(r) for r in airframes],
            "airports": [wb.shape_search_airport(r) for r in airports],
        }
