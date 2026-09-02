import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402
from app.vinted_client import VintedClient  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        API_KEY="test-key",
        VINTED_PROXY=None,
        VINTED_MIN_INTERVAL=0,
        VINTED_COOKIE_FILE=str(tmp_path / "cookies.json"),
    )


@pytest.fixture
def client(settings: Settings) -> VintedClient:
    return VintedClient(settings)


@pytest.fixture
def item_page_html() -> str:
    """Trimmed Vinted item page carrying every element the parser reads."""
    return """
<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Nike Air Max 90 | Vinted">
  <meta name="description" content="Baskets Nike en tres bon etat">
  <script type="application/ld+json">
  {"@type": "Product", "name": "Nike Air Max 90",
   "description": "Baskets Nike en tres bon etat",
   "image": "https://images.vinted.net/main.jpg",
   "color": "Blanc",
   "brand": {"name": "Nike"},
   "offers": {"price": 45.0, "priceCurrency": "EUR",
              "url": "https://www.vinted.fr/items/123456-nike-air-max-90",
              "itemCondition": "https://schema.org/UsedCondition",
              "availability": "https://schema.org/InStock"}}
  </script>
</head>
<body>
  <div data-testid="item-price">45,00 &euro;</div>
  <div data-testid="total-combined-price">48,50 &euro;</div>
  <div data-testid="item-shipping-banner-price">3,50 &euro;</div>
  <div data-testid="service-fee-included-title">Frais inclus</div>

  <div data-testid="item-attributes-size"><span itemProp="size">
    <span class="web_ui__Text__bold">42</span></span></div>
  <div data-testid="item-attributes-status"><span itemProp="status">
    <span class="web_ui__Text__bold">Tres bon etat</span></span></div>
  <div data-testid="item-attributes-color"><span itemProp="color">
    <span class="web_ui__Text__bold">Blanc</span></span></div>
  <div data-testid="item-attributes-upload_date"><span itemProp="upload_date">
    <span class="web_ui__Text__bold">il y a 2 jours</span></span></div>

  <img class="item-photo-1" src="https://images.vinted.net/photo1.jpg">
  <img class="item-photo-2" src="https://images.vinted.net/photo2.jpg">
  <img class="unrelated-banner" src="https://images.vinted.net/ad.jpg">

  <a href="/brand/53-nike">Nike</a>
  <a href="/member/9988776">Profil</a>
  <a href="/inbox/new?receiver_id=9988776">Message</a>
  <div data-testid="profile-username">supervendeur</div>
  <div data-testid="seller-location">Paris, France</div>
  <div data-testid="seller-last-logged-in">il y a 3 heures</div>
  <div aria-label="supervendeur noté 4.8 sur 5"></div>

  <a href="/catalog/1904-chaussures" itemProp="url"><span itemProp="title">Chaussures</span></a>
  <a href="/catalog/16-hommes" itemProp="url"><span itemProp="title">Hommes</span></a>
</body>
</html>
"""
