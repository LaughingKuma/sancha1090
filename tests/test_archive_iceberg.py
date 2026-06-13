from __future__ import annotations

from pyiceberg.transforms import DayTransform

from include import archive_iceberg
from include import iceberg as states_iceberg


def test_archive_schema_mirrors_opensky_states_plus_source():
    # dbt history models union with the live lane column-for-column; drift here
    # breaks agg_hourly_traffic silently.
    states_fields = list(states_iceberg.SCHEMA.fields)
    archive_fields = list(archive_iceberg.SCHEMA.fields)
    assert len(archive_fields) == len(states_fields) + 1
    for sf, af in zip(states_fields, archive_fields, strict=False):
        assert (sf.field_id, sf.name, sf.field_type, sf.required) == (
            af.field_id, af.name, af.field_type, af.required
        )
    extra = archive_fields[-1]
    assert (extra.field_id, extra.name) == (21, "source")
    assert len({f.field_id for f in archive_fields}) == len(archive_fields)


def test_archive_partitioned_by_snapshot_day():
    spec = archive_iceberg.PARTITION_SPEC
    assert len(spec.fields) == 1
    field = spec.fields[0]
    assert field.source_id == 17
    assert isinstance(field.transform, DayTransform)
    assert field.name == "snapshot_day"
    # Guard against field-id drift: 17 must remain snapshot_time or the partition
    # silently keys on the wrong column.
    assert archive_iceberg.SCHEMA.find_field(17).name == "snapshot_time"


def test_archive_table_identity():
    assert archive_iceberg.QUALIFIED == "bronze.archive_states"
