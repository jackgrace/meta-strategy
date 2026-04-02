import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Meta API
    meta_access_token: str = ""
    meta_ad_account_id: str = ""  # Format: act_XXXXXXXXX

    # Slack
    slack_webhook_url: str = ""

    # Analysis
    lookback_days: int = 7  # Total window to pull data
    baseline_days: int = 4  # First N days = baseline
    recent_days: int = 3    # Last N days = recent performance
    min_spend_threshold: float = 5.0  # Ignore ads spending less than $X/day avg
    min_impressions: int = 100  # Minimum impressions to evaluate

    # Fatigue score thresholds
    critical_threshold: int = 75
    warning_threshold: int = 50
    watch_threshold: int = 30

    # Signal weights for EFFICIENCY ads (high ROAS, bottom-funnel role)
    efficiency_weights: dict = field(default_factory=lambda: {
        "ctr_decay": 0.15,
        "cpc_inflation": 0.10,
        "cpm_inflation": 0.15,
        "frequency_climb": 0.20,
        "roas_decay": 0.30,
        "spend_share_decline": 0.10,
    })

    # Signal weights for ENGAGEMENT ads (low ROAS but strong CTR/CPC, ASC likes them)
    engagement_weights: dict = field(default_factory=lambda: {
        "ctr_decay": 0.30,
        "cpc_inflation": 0.20,
        "cpm_inflation": 0.15,
        "frequency_climb": 0.25,
        "roas_decay": 0.05,
        "spend_share_decline": 0.05,
    })

    # Role classification thresholds
    efficiency_roas_floor: float = 2.0  # Above this = efficiency ad
    engagement_ctr_floor: float = 1.5   # Above this CTR% + low ROAS = engagement ad

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            meta_access_token=os.environ["META_ACCESS_TOKEN"],
            meta_ad_account_id=os.environ["META_AD_ACCOUNT_ID"],
            slack_webhook_url=os.environ["SLACK_WEBHOOK_URL"],
        )

        # Optional overrides
        if v := os.environ.get("LOOKBACK_DAYS"):
            cfg.lookback_days = int(v)
        if v := os.environ.get("MIN_SPEND_THRESHOLD"):
            cfg.min_spend_threshold = float(v)
        if v := os.environ.get("EFFICIENCY_ROAS_FLOOR"):
            cfg.efficiency_roas_floor = float(v)
        if v := os.environ.get("ENGAGEMENT_CTR_FLOOR"):
            cfg.engagement_ctr_floor = float(v)

        return cfg
