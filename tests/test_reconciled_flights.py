from __future__ import annotations


def _q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def test_flight_id_unique(ch_cur):
    # Grain: one row per flight_id.
    dupes = _q(ch_cur, "SELECT count() FROM (SELECT flight_id FROM gold_ch.fct_flights_reconciled GROUP BY flight_id HAVING count() > 1)")[0][0]
    assert dupes == 0, f"{dupes} duplicate flight_id"


def test_plurality_honored(ch_cur):
    # Consensus = plurality: a non-curated resolved endpoint must be a top-vote airport in its votes map.
    for airport, source, votes in (("origin_icao","origin_source","origin_votes"), ("dest_icao","dest_source","dest_votes")):
        bad = _q(ch_cur, f"SELECT count() FROM gold_ch.fct_flights_reconciled "
                         f"WHERE {airport} IS NOT NULL AND {source} != 'curated' AND length({votes}) > 0 "
                         f"AND {votes}[{airport}] < arrayMax(mapValues({votes}))")[0][0]
        assert bad == 0, f"{bad} {airport} rows resolved to a non-plurality airport"


def test_consensus_resolves_real_disagreements_by_majority(ch_cur):
    # The SP1 win: genuine multi-airport contests get resolved by majority vote (not single-source pass-through).
    n = _q(ch_cur, "SELECT count() FROM gold_ch.fct_flights_reconciled WHERE dest_agreement='majority' AND length(dest_votes) >= 2")[0][0]
    assert n > 0, "no majority-resolved multi-airport dest contests — consensus not firing"


def test_rjec_civilian_beats_rjca_military_when_outvoted(ch_cur):
    # Civ/mil disambiguation: where RJEC (civ) out-votes RJCA (JGSDF) in a genuine two-way contest,
    # consensus must pick RJEC. Both-present guard required -- Map[] defaults a missing key to 0.
    contest = "mapContains(dest_votes,'RJCA') AND mapContains(dest_votes,'RJEC')"
    bad = _q(ch_cur, f"SELECT count() FROM gold_ch.fct_flights_reconciled "
                     f"WHERE {contest} AND dest_votes['RJEC'] > dest_votes['RJCA'] AND dest_icao != 'RJEC' AND dest_source != 'curated'")[0][0]
    assert bad == 0, f"{bad} flights had RJEC out-voting RJCA yet did not resolve RJEC"
    fires = _q(ch_cur, f"SELECT count() FROM gold_ch.fct_flights_reconciled WHERE {contest} AND dest_votes['RJEC'] > dest_votes['RJCA'] AND dest_icao = 'RJEC'")[0][0]
    assert fires > 0, "the RJEC-over-RJCA resolution never fires"


def test_rjec_mislabel_regression_anchors(ch_cur):
    # Historical RJEC-vs-RJCA mislabel anchors: pin the stable OUTCOME (RJEC wins via consensus or sched-service
    # tiebreak), not the vote margin — the opensky_states RJEC vote ages out of the ~30-day context feed.
    ado = _q(ch_cur, "SELECT dest_icao FROM gold_ch.fct_flights_reconciled "
                     "WHERE icao24='861b64' AND callsign='ADO81' AND toDate(start_time)='2026-06-06'")
    assert ado and all(r[0]=='RJEC' for r in ado), f"ADO81 anchor not RJEC: {ado}"
    jal = _q(ch_cur, "SELECT dest_icao FROM gold_ch.fct_flights_reconciled "
                     "WHERE icao24='851c20' AND callsign='JAL551' AND toDate(start_time)='2026-07-02'")
    assert jal and all(r[0]=='RJEC' for r in jal), f"JAL551 851c20 anchor not RJEC: {jal}"


def test_reconciled_same_airport_below_legacy_blend(ch_cur):
    # SP2: fct_flight_legs is now snap-only (~3% same-airport, but resolves far fewer routes), so the SP1
    # "reconciled < fct_flight_legs" framing inverts. Durable guard instead: reconciled's collapsed
    # origin==dest rate stays well below the SP1-era BLENDED-legs baseline (~14.1%). ~6.5% today; 0.12 is a
    # loose ceiling (a rate bound, not a row count) that still catches a real regression toward the old blend.
    r = _q(ch_cur, "SELECT countIf(origin_icao IS NOT NULL AND origin_icao=dest_icao)/count() FROM gold_ch.fct_flights_reconciled")[0][0]
    assert r < 0.12, f"reconciled same-airport {r} not below the ~14.1% legacy blended-legs baseline"


def test_tiebreaks_are_genuine(ch_cur):
    # Transparency contract: a 'tiebreak' row genuinely has >=2 airports sharing the top vote count.
    bad = _q(ch_cur, "SELECT count() FROM gold_ch.fct_flights_reconciled "
                     "WHERE dest_agreement='tiebreak' AND dest_source != 'curated' "
                     "AND arrayCount(x -> x = arrayMax(mapValues(dest_votes)), mapValues(dest_votes)) < 2")[0][0]
    assert bad == 0, f"{bad} 'tiebreak' dest rows lack a genuine >=2-airport tie"


def test_curated_override_applied(ch_cur):
    # The curated override layer fires and is labeled.
    n = _q(ch_cur, "SELECT count() FROM gold_ch.fct_flights_reconciled WHERE origin_source='curated' OR dest_source='curated'")[0][0]
    assert n > 0, "no curated overrides present"


def test_agreement_spread(ch_cur):
    # Consensus produces a spread of agreement levels, not one degenerate label.
    kinds = _q(ch_cur, "SELECT uniqExact(origin_agreement) FROM gold_ch.fct_flights_reconciled WHERE origin_agreement IS NOT NULL")[0][0]
    assert kinds >= 3, "origin_agreement has <3 distinct values — consensus likely degenerate"


def test_tokyo_airports_still_endpoints(ch_cur):
    n = _q(ch_cur, "SELECT count() FROM gold_ch.fct_flights_reconciled WHERE origin_icao IN ('RJTT','RJAA') OR dest_icao IN ('RJTT','RJAA')")[0][0]
    assert n > 0, "no reconciled flights touch Tokyo airports"


def test_no_airline_tiebreak_to_unscheduled(ch_cur):
    # Fix B guarantee: an airline-shaped tiebreak may only land unscheduled when NO scheduled airport was
    # among the top-vote-tied candidates -- if one was, the sched preference must have picked it.
    for airport, agr, votes in (("origin_icao", "origin_agreement", "origin_votes"),
                                ("dest_icao", "dest_agreement", "dest_votes")):
        bad = _q(ch_cur, f"SELECT count() FROM ("
                         f"  SELECT flight_id FROM ("
                         f"    SELECT r.flight_id AS flight_id, r.{airport} AS won,"
                         f"      arrayJoin(arrayFilter(k -> r.{votes}[k] = arrayMax(mapValues(r.{votes})), mapKeys(r.{votes}))) AS cand"
                         f"    FROM gold_ch.fct_flights_reconciled r"
                         f"    WHERE r.{agr} = 'tiebreak' AND r.{airport.split('_')[0]}_source != 'curated'"
                         f"      AND match(trimBoth(r.callsign), '^[A-Z]{{3}}[0-9]')"
                         f"  ) t LEFT JOIN silver_ch.dim_airports ap ON ap.icao = t.cand"
                         f"  GROUP BY flight_id, won"
                         f"  HAVING maxIf(coalesce(ap.scheduled_service, false), t.cand = won) = false"
                         f"     AND max(coalesce(ap.scheduled_service, false)) = true"
                         f")")[0][0]
        assert bad == 0, f"{bad} airline-shaped {airport} tiebreaks unscheduled despite a scheduled tied candidate"


def test_relevance_scope_applied(ch_cur):
    # a regressed relevance filter would re-inflate the mart back to the global spine, or wrongly drop a
    # Japan-airport (opensky_flights) summary; both are checked against same-lane tables (no live-feed race).
    row = _q(ch_cur, "SELECT (SELECT count() FROM gold_ch.fct_flights_reconciled), (SELECT count() FROM silver_ch.int_flight_spine)")[0]
    mart, spine = row[0], row[1]
    assert mart < spine, f"relevance filter inactive: mart {mart} vs global spine {spine}"
    dropped = _q(ch_cur, "SELECT count() FROM silver_ch.int_flight_spine sp WHERE sp.anchor_source = 'opensky_flights' "
                         "AND sp.flight_id NOT IN (SELECT flight_id FROM gold_ch.fct_flights_reconciled)")[0][0]
    assert dropped == 0, f"{dropped} opensky_flights summaries dropped by the relevance filter"
