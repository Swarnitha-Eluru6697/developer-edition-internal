from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.topic_policy import CLEANUP_POLICIES, COMPRESSION_TYPES, SCHEMA_STRATEGIES


class TopicPolicyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None

    # When is_domain_default is set, this policy is the fallback for every
    # pipeline in `domain` that doesn't set its own topic_policy_id — see
    # app/services/topic_policy.py's resolve_topic_config.
    domain: str | None = None
    is_domain_default: bool = False

    # Every knob below is optional: a policy only needs to override the
    # fields it cares about, and inherits the rest from the platform
    # defaults (settings.topic_default_*) at resolution time.
    partitions: int | None = Field(default=None, ge=1)
    replication_factor: int | None = Field(default=None, ge=1)
    retention_ms: int | None = Field(default=None, ge=-1)
    retention_bytes: int | None = Field(default=None, ge=-1)
    cleanup_policy: str | None = None
    min_insync_replicas: int | None = Field(default=None, ge=1)
    compression_type: str | None = None
    max_message_bytes: int | None = Field(default=None, ge=1)
    schema_strategy: str | None = None

    @model_validator(mode="after")
    def validate_min_isr_within_replication(self) -> "TopicPolicyBase":
        # GAP-06: needs no cluster. min.insync.replicas is "how many of the replicas must acknowledge", so it can never
        # exceed how many replicas there are. (Checked only when BOTH are set here; a field left null falls through to
        # the platform default, and the resolved pair is checked again at readiness/deploy.)
        if (
            self.replication_factor is not None
            and self.min_insync_replicas is not None
            and self.min_insync_replicas > self.replication_factor
        ):
            raise ValueError(
                f"min_insync_replicas ({self.min_insync_replicas}) cannot exceed replication_factor "
                f"({self.replication_factor})"
            )
        return self

    @field_validator("cleanup_policy")
    @classmethod
    def validate_cleanup_policy(cls, v: str | None) -> str | None:
        if v is not None and v not in CLEANUP_POLICIES:
            raise ValueError(f"cleanup_policy must be one of {sorted(CLEANUP_POLICIES)}")
        return v

    @field_validator("compression_type")
    @classmethod
    def validate_compression_type(cls, v: str | None) -> str | None:
        if v is not None and v not in COMPRESSION_TYPES:
            raise ValueError(f"compression_type must be one of {sorted(COMPRESSION_TYPES)}")
        return v

    @field_validator("schema_strategy")
    @classmethod
    def validate_schema_strategy(cls, v: str | None) -> str | None:
        if v is not None and v not in SCHEMA_STRATEGIES:
            raise ValueError(f"schema_strategy must be one of {sorted(SCHEMA_STRATEGIES)}")
        return v


class TopicPolicyCreate(TopicPolicyBase):
    id: str | None = None


class TopicPolicy(TopicPolicyBase):
    id: str
    org_id: str
