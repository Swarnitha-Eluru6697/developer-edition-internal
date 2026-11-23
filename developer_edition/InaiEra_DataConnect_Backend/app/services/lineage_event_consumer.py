from app.services.kafka_runtime import KafkaConsumer, KafkaRecord
from app.services.lineage_service import LineageService, lineage_service


class LineageEventConsumer:
    def __init__(
        self,
        consumer: KafkaConsumer,
        service: LineageService = lineage_service,
    ):
        self.consumer = consumer
        self.service = service

    async def consume_once(self) -> dict | None:
        record = await self.consumer.poll()
        if record is None:
            return None
        return await self.process_record(record)

    async def process_record(self, record: KafkaRecord) -> dict:
        return await self.service.consume_runtime_event(record.value)
