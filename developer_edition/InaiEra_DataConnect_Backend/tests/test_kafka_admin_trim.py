"""kafka_admin.trim_consumed_records: an ENFORCE sink's hand-off records are deleted up to what its connector's consumer group has
committed — never beyond, never another topic's, and not again for a partition with nothing new."""
from types import SimpleNamespace

from kafka import TopicPartition

from app.services import kafka_admin

HANDOFF = "_dataconnect.gate.shop-pg-orders_copy-stream-v1"


class _Admin:
    deleted: list = []

    def __init__(self, **kwargs):
        pass

    def list_group_offsets(self, specs):
        (group,) = specs
        return {group: {TopicPartition(HANDOFF, 0): SimpleNamespace(offset=49), TopicPartition(HANDOFF, 1): SimpleNamespace(offset=12),
                        TopicPartition("other-topic", 0): SimpleNamespace(offset=99)}}

    def delete_records(self, to_delete):
        _Admin.deleted.append(dict(to_delete))

    def close(self):
        pass


def test_trims_only_the_hand_off_topic_up_to_the_committed_offsets(monkeypatch) -> None:
    _Admin.deleted = []
    monkeypatch.setattr(kafka_admin, "KafkaAdminClient", _Admin)

    done = kafka_admin._trim_consumed_records_sync(HANDOFF, "connect-x", already={1: 12})

    assert done == {0: 49}                                                 # partition 1 has nothing new since the last trim
    assert _Admin.deleted == [{TopicPartition(HANDOFF, 0): 49}]          # never "other-topic"


def test_nothing_new_deletes_nothing(monkeypatch) -> None:
    _Admin.deleted = []
    monkeypatch.setattr(kafka_admin, "KafkaAdminClient", _Admin)

    assert kafka_admin._trim_consumed_records_sync(HANDOFF, "connect-x", already={0: 49, 1: 12}) == {}
    assert _Admin.deleted == []
