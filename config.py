import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # Meta API
    meta_access_token: str = ""
    meta_ad_account_id: str = ""  # Format: act_XXXXXXXXX

    # Slack
    slack_webhook_url: str = ""

    # Analysis — short window (acute fatigue)
    lookback_days: int = 7  # Total short window
    baseline_days: int = 4  # First N days = baseline
    recent_days: int = 3    # Last N days = recent performance

    # Analysis — long window (slow-burn fatigue)
    long_lookback_days: int = 21  # Total long window
    long_baseline_days: int = 14  # First N days = baseline
    long_recent_days: int = 7     # Last N days = recent performance
    long_trend_threshold: int = 25  # Min long-window score to flag slow burn

    min_spend_threshold: float = 5.0  # Ignore ads spending less than $X/day avg
    min_impressions: int = 100  # Minimum impressions to evaluate

    # Fatigue score thresholds
    critical_threshold: int = 75
    warning_threshold: int = 50
    watch_threshold: int = 30

    # Signal weights for EFFICIENCY ads (high ROAS, bottom-funnel role)
    # CPA is heavily weighted here — rising CPA is the earliest fatigue signal
    # for bottom-funnel ads because it moves before ROAS (AOV fluctuates)
    efficiency_weights: dict = field(default_factory=lambda: {
        "ctr_decay": 0.10,
        "cpc_inflation": 0.05,
        "cpm_inflation": 0.10,
        "cpa_inflation": 0.20,
        "frequency_climb": 0.15,
        "roas_decay": 0.25,
        "spend_share_decline": 0.15,
    })

    # Signal weights for ENGAGEMENT ads (low ROAS but strong CTR/CPC, ASC likes them)
    # CPA matters less here — these ads often have high CPA by design
    engagement_weights: dict = field(default_factory=lambda: {
        "ctr_decay": 0.25,
        "cpc_inflation": 0.15,
        "cpm_inflation": 0.15,
        "cpa_inflation": 0.05,
        "frequency_climb": 0.25,
        "roas_decay": 0.05,
        "spend_share_decline": 0.10,
    })

    # Role classification thresholds
    efficiency_roas_floor: float = 2.0  # Above this = efficiency ad
    engagement_ctr_floor: float = 1.5   # Above this CTR% + low ROAS = engagement ad

    # Daily ROAS warning check — separate from fatigue analysis
    roas_warning_min_spend_7d: float = 200.0  # Only flag ads spending > $X over 7 days
    roas_warning_threshold: float = 1.6       # Flag ads with ROAS below this

    # Testing campaign missed opportunity check
    testing_campaign_keyword: str = "TESTING"  # Campaign name must contain this

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
        if v := os.environ.get("ROAS_WARNING_MIN_SPEND_7D"):
            cfg.roas_warning_min_spend_7d = float(v)
        if v := os.environ.get("ROAS_WARNING_THRESHOLD"):
            cfg.roas_warning_threshold = float(v)

        return cfg
