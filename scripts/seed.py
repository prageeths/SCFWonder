"""Seed SCF Wonder with a realistic dataset.

Run:  python -m scripts.seed [invoice_count]
"""
from __future__ import annotations

import datetime as _dt
import random
import sys
import uuid
from pathlib import Path
from typing import Dict, List

sys.path.append(str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from app import models  # noqa: E402
from app.config import BASE_RATE, FX_TO_USD, PRODUCT_FACTORING, PRODUCT_REVERSE_FACTORING, SUPPORTED_CURRENCIES  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.tools._common import log_event  # noqa: E402
from app.tools.credit_limit_tools import tool_ensure_limits  # noqa: E402
from app.tools.underwriting_tools import tool_build_risk_profile  # noqa: E402

RNG_SEED = 17
random.seed(RNG_SEED)


# ----- hierarchical buyers -----
BUYER_HIERARCHIES = [
    ("Walmart", "Walmart Global", None, "Retail", "BOTH", 611_000_000_000),
    ("Walmart", "Walmart US", "US", "Retail", "BOTH", 420_000_000_000),
    ("Walmart", "Walmart Canada", "CA", "Retail", "BUYER", 22_000_000_000),
    ("Walmart", "Walmart LATAM", None, "Retail", "BUYER", 40_000_000_000),
    ("Walmart", "Walmart Mexico", "MX", "Retail", "BUYER", 30_000_000_000),
    ("Walmart", "Walmart Brazil", "BR", "Retail", "BUYER", 5_000_000_000),
    ("Walmart", "Walmart Colombia", "CO", "Retail", "BUYER", 3_000_000_000),
    ("Walmart", "Walmart East Coast", "US", "Retail", "BUYER", 110_000_000_000),
    ("Walmart", "Walmart Midwest", "US", "Retail", "BUYER", 95_000_000_000),
    ("Walmart", "Walmart West Coast", "US", "Retail", "BUYER", 130_000_000_000),
    ("Walmart", "Walmart South", "US", "Retail", "BUYER", 85_000_000_000),
    ("Target", "Target Corporation", "US", "Retail", "BUYER", 109_000_000_000),
    ("Target", "Target East", "US", "Retail", "BUYER", 35_000_000_000),
    ("Target", "Target West", "US", "Retail", "BUYER", 38_000_000_000),
    ("Target", "Target South", "US", "Retail", "BUYER", 22_000_000_000),
    ("Target", "Target Midwest", "US", "Retail", "BUYER", 14_000_000_000),
    ("Kroger", "Kroger Co", "US", "Retail", "BUYER", 148_000_000_000),
    ("Kroger", "Kroger Mid-Atlantic", "US", "Retail", "BUYER", 22_000_000_000),
    ("Kroger", "Kroger Southwest", "US", "Retail", "BUYER", 25_000_000_000),
    ("Kroger", "Kroger Central", "US", "Retail", "BUYER", 30_000_000_000),
    ("Kroger", "Kroger Delta", "US", "Retail", "BUYER", 18_000_000_000),
    ("Albertsons", "Albertsons Companies", "US", "Retail", "BUYER", 79_000_000_000),
    ("Albertsons", "Jewel-Osco", "US", "Retail", "BUYER", 6_500_000_000),
    ("Albertsons", "Safeway", "US", "Retail", "BUYER", 38_000_000_000),
    ("Albertsons", "Vons", "US", "Retail", "BUYER", 5_400_000_000),
    ("BestBuy", "Best Buy Co", "US", "Consumer Electronics", "BUYER", 46_000_000_000),
    ("BestBuy", "Best Buy US", "US", "Consumer Electronics", "BUYER", 42_000_000_000),
    ("BestBuy", "Best Buy Canada", "CA", "Consumer Electronics", "BUYER", 4_000_000_000),
    ("Costco", "Costco Wholesale", "US", "Retail", "BUYER", 242_000_000_000),
    ("Costco", "Costco US", "US", "Retail", "BUYER", 180_000_000_000),
    ("Costco", "Costco Canada", "CA", "Retail", "BUYER", 25_000_000_000),
    ("Costco", "Costco Mexico", "MX", "Retail", "BUYER", 6_000_000_000),
    ("CVS", "CVS Health", "US", "Pharmaceuticals", "BUYER", 357_000_000_000),
    ("CVS", "CVS Pharmacy", "US", "Pharmaceuticals", "BUYER", 110_000_000_000),
    ("Walgreens", "Walgreens Boots Alliance", "US", "Pharmaceuticals", "BUYER", 139_000_000_000),
    ("Walgreens", "Walgreens US", "US", "Pharmaceuticals", "BUYER", 110_000_000_000),
    ("Amazon", "Amazon", "US", "Retail", "BUYER", 575_000_000_000),
    ("Amazon", "Whole Foods Market", "US", "Retail", "BUYER", 22_000_000_000),
    ("Amazon", "Amazon Fresh", "US", "Retail", "BUYER", 10_000_000_000),
    ("Publix", "Publix Super Markets", "US", "Retail", "BUYER", 57_000_000_000),
    ("Wegmans", "Wegmans Food Markets", "US", "Retail", "BUYER", 12_000_000_000),
    ("TraderJoes", "Trader Joe's", "US", "Retail", "BUYER", 16_000_000_000),
    ("Aldi", "Aldi US", "US", "Retail", "BUYER", 35_000_000_000),
    ("HEB", "H-E-B", "US", "Retail", "BUYER", 38_000_000_000),
    ("Meijer", "Meijer", "US", "Retail", "BUYER", 21_000_000_000),
    ("WinnDixie", "Winn-Dixie", "US", "Retail", "BUYER", 4_000_000_000),
]

KNOWN_SELLERS = [
    ("Kellogg Company", "Food & Beverage", "US", 14_000_000_000),
    ("Quaker Oats Company", "Food & Beverage", "US", 2_500_000_000),
    ("General Mills", "Food & Beverage", "US", 20_000_000_000),
    ("Post Holdings", "Food & Beverage", "US", 6_500_000_000),
    ("Mondelez International", "Food & Beverage", "US", 36_000_000_000),
    ("Nestle USA", "Food & Beverage", "US", 28_000_000_000),
    ("Kraft Heinz", "Food & Beverage", "US", 27_000_000_000),
    ("Conagra Brands", "Food & Beverage", "US", 12_000_000_000),
    ("Tyson Foods", "Food & Beverage", "US", 53_000_000_000),
    ("Hormel Foods", "Food & Beverage", "US", 12_400_000_000),
    ("Campbell Soup Company", "Food & Beverage", "US", 9_400_000_000),
    ("Hershey Company", "Food & Beverage", "US", 10_000_000_000),
    ("Mars Incorporated", "Food & Beverage", "US", 45_000_000_000),
    ("The Coca-Cola Company", "Food & Beverage", "US", 45_000_000_000),
    ("PepsiCo", "Food & Beverage", "US", 91_000_000_000),
    ("Keurig Dr Pepper", "Food & Beverage", "US", 14_800_000_000),
    ("Monster Beverage", "Food & Beverage", "US", 7_000_000_000),
    ("Red Bull North America", "Food & Beverage", "US", 5_000_000_000),
    ("Anheuser-Busch", "Food & Beverage", "US", 16_000_000_000),
    ("Molson Coors Beverage Co", "Food & Beverage", "US", 11_000_000_000),
    ("Constellation Brands", "Food & Beverage", "US", 9_400_000_000),
    ("Brown-Forman", "Food & Beverage", "US", 4_000_000_000),
    ("Reckitt Benckiser (Delsym)", "Pharmaceuticals", "US", 16_000_000_000),
    ("Pfizer Consumer Health (Advil)", "Pharmaceuticals", "US", 4_000_000_000),
    ("Johnson & Johnson Consumer", "Pharmaceuticals", "US", 15_000_000_000),
    ("Bayer Consumer Health", "Pharmaceuticals", "US", 5_000_000_000),
    ("GSK Consumer Healthcare", "Pharmaceuticals", "US", 11_000_000_000),
    ("Procter & Gamble Health", "Pharmaceuticals", "US", 6_000_000_000),
    ("Sanofi Consumer Healthcare", "Pharmaceuticals", "US", 5_500_000_000),
    ("Haleon", "Pharmaceuticals", "US", 12_000_000_000),
    ("Thermos LLC", "Home Goods", "US", 600_000_000),
    ("OXO International", "Home Goods", "US", 350_000_000),
    ("Newell Brands", "Home Goods", "US", 8_500_000_000),
    ("Tupperware Brands", "Home Goods", "US", 1_200_000_000),
    ("Hamilton Beach", "Home Goods", "US", 700_000_000),
    ("HP Inc", "Consumer Electronics", "US", 53_000_000_000),
    ("Dell Technologies", "Consumer Electronics", "US", 102_000_000_000),
    ("Lenovo USA", "Consumer Electronics", "US", 14_000_000_000),
    ("Logitech Inc", "Consumer Electronics", "US", 4_500_000_000),
    ("Bose Corporation", "Consumer Electronics", "US", 3_700_000_000),
    ("Garmin International", "Consumer Electronics", "US", 5_200_000_000),
    ("Sonos", "Consumer Electronics", "US", 1_700_000_000),
    ("Roku", "Consumer Electronics", "US", 3_500_000_000),
    ("GoPro", "Consumer Electronics", "US", 1_000_000_000),
    ("Fossil Group", "Watches & Accessories", "US", 1_200_000_000),
    ("Movado Group", "Watches & Accessories", "US", 700_000_000),
    ("Timex Group USA", "Watches & Accessories", "US", 800_000_000),
    ("Citizen Watch America", "Watches & Accessories", "US", 600_000_000),
    ("Bulova Corporation", "Watches & Accessories", "US", 250_000_000),
    ("Shinola Detroit", "Watches & Accessories", "US", 100_000_000),
    ("Skagen Designs", "Watches & Accessories", "US", 80_000_000),
    ("Levi Strauss & Co", "Apparel", "US", 6_200_000_000),
    ("VF Corporation", "Apparel", "US", 11_600_000_000),
    ("PVH Corp", "Apparel", "US", 9_200_000_000),
    ("Hanesbrands", "Apparel", "US", 6_200_000_000),
    ("Under Armour", "Apparel", "US", 5_700_000_000),
    ("Carter's Inc", "Apparel", "US", 3_000_000_000),
    ("Columbia Sportswear", "Apparel", "US", 3_500_000_000),
    ("Crocs Inc", "Apparel", "US", 3_900_000_000),
    ("Skechers USA", "Apparel", "US", 7_500_000_000),
    ("Blue Buffalo", "Food & Beverage", "US", 1_400_000_000),
    ("Spectrum Brands Pet", "Food & Beverage", "US", 900_000_000),
    ("Clorox Company", "Home Goods", "US", 7_400_000_000),
    ("Church & Dwight", "Home Goods", "US", 5_400_000_000),
    ("Energizer Holdings", "Home Goods", "US", 2_900_000_000),
    ("Edgewell Personal Care", "Home Goods", "US", 2_300_000_000),
]

TIER2_SAMPLE = [
    ("Sealed Air Corporation", "Packaging", "US", 5_500_000_000),
    ("Berry Global", "Packaging", "US", 12_700_000_000),
    ("Sonoco Products", "Packaging", "US", 7_200_000_000),
    ("WestRock Company", "Packaging", "US", 21_000_000_000),
    ("Packaging Corp of America", "Packaging", "US", 8_400_000_000),
    ("ADM (Archer Daniels Midland)", "Ingredients", "US", 102_000_000_000),
    ("Ingredion Incorporated", "Ingredients", "US", 8_200_000_000),
    ("Cargill Inc", "Ingredients", "US", 165_000_000_000),
    ("Bunge Limited", "Ingredients", "US", 67_000_000_000),
    ("International Flavors & Fragrances", "Ingredients", "US", 12_400_000_000),
    ("Givaudan US", "Ingredients", "US", 7_000_000_000),
    ("Symrise US", "Ingredients", "US", 4_700_000_000),
    ("J.B. Hunt Transport Services", "Logistics", "US", 14_800_000_000),
    ("XPO Logistics", "Logistics", "US", 7_700_000_000),
    ("Old Dominion Freight Line", "Logistics", "US", 5_900_000_000),
    ("C.H. Robinson Worldwide", "Logistics", "US", 24_700_000_000),
    ("Schneider National", "Logistics", "US", 5_500_000_000),
]


def _seed_companies(db: Session) -> Dict[str, models.Company]:
    by_name: Dict[str, models.Company] = {}
    group_root: Dict[str, models.Company] = {}

    for group_key, name, country, industry, role, revenue in BUYER_HIERARCHIES:
        c = models.Company(
            name=name, legal_name=name, country=country, industry=industry, role=role,
            tax_id=f"EIN-{random.randint(10000000, 99999999)}",
            website=f"https://www.{name.lower().replace(' ', '').replace('-', '').replace(',','')[:20]}.com",
            founded_year=random.randint(1900, 2010),
            employees=int(max(100, revenue / random.uniform(150_000, 600_000))),
            annual_revenue_usd=float(revenue),
            description=f"{name} — {industry} buyer.",
        )
        db.add(c); db.flush()
        by_name[name] = c
        if group_key not in group_root:
            group_root[group_key] = c
        else:
            parent = group_root[group_key]
            if group_key == "Walmart":
                if name in ("Walmart Mexico", "Walmart Brazil", "Walmart Colombia"):
                    parent = by_name.get("Walmart LATAM", parent)
                elif name in ("Walmart East Coast", "Walmart Midwest",
                              "Walmart West Coast", "Walmart South"):
                    parent = by_name.get("Walmart US", parent)
                elif name in ("Walmart US", "Walmart Canada", "Walmart LATAM"):
                    parent = by_name.get("Walmart Global", parent)
            elif group_key == "Albertsons":
                parent = by_name.get("Albertsons Companies", parent)
            elif group_key == "CVS":
                parent = by_name.get("CVS Health", parent)
            elif group_key == "Walgreens":
                parent = by_name.get("Walgreens Boots Alliance", parent)
            elif group_key == "Amazon":
                parent = by_name.get("Amazon", parent)
            c.parent_id = parent.id
            db.flush()

    for name, industry, country, revenue in KNOWN_SELLERS + TIER2_SAMPLE:
        role = "SELLER" if (name, industry, country, revenue) in KNOWN_SELLERS else "BOTH"
        c = models.Company(
            name=name, legal_name=name, country=country, industry=industry, role=role,
            tax_id=f"EIN-{random.randint(10000000, 99999999)}",
            website=f"https://www.{name.lower().split()[0]}.com",
            founded_year=random.randint(1900, 2015),
            employees=int(max(50, revenue / random.uniform(150_000, 800_000))),
            annual_revenue_usd=float(revenue),
            description=f"{name} — {industry}.",
        )
        db.add(c); db.flush()
        by_name[name] = c
    return by_name


def _generate_extra_sellers(db: Session, by_name: Dict[str, models.Company], n_target: int) -> None:
    try:
        from faker import Faker
    except ImportError:
        Faker = None
    fake = Faker("en_US") if Faker else None
    if fake:
        Faker.seed(RNG_SEED)

    industries = ["Food & Beverage", "Pharmaceuticals", "Consumer Electronics",
                  "Apparel", "Home Goods", "Watches & Accessories", "Packaging",
                  "Ingredients", "Logistics", "Industrial"]
    suffixes = ["Inc", "LLC", "Corp", "Co", "Holdings", "Group", "Brands", "Industries"]
    descriptors = ["Foods", "Beverages", "Snacks", "Naturals", "Organics", "Pharma",
                   "Health", "Tech", "Electronics", "Apparel", "Wearables", "Home",
                   "Goods", "Packaging", "Ingredients", "Logistics", "Distribution",
                   "Bakery", "Dairy", "Coffee", "Tea", "Spirits", "Brewing"]

    existing = {c.name for c in by_name.values() if c.role in ("SELLER", "BOTH")}
    needed = max(0, n_target - len(existing))
    print(f"[seed] Generating {needed} synthetic sellers...")

    created, attempts = 0, 0
    while created < needed and attempts < needed * 5:
        attempts += 1
        base = (fake.last_name() + " " + random.choice(descriptors)) if fake \
               else f"Acme {random.choice(descriptors)}-{attempts}"
        name = f"{base} {random.choice(suffixes)}"
        if name in by_name:
            continue
        industry = random.choice(industries)
        revenue = random.choice([
            random.uniform(2e7, 5e8), random.uniform(5e8, 5e9), random.uniform(5e9, 2e10),
        ])
        c = models.Company(
            name=name, legal_name=name, country="US", industry=industry, role="SELLER",
            tax_id=f"EIN-{random.randint(10000000, 99999999)}",
            website=f"https://{name.lower().split()[0]}.example.com",
            founded_year=random.randint(1950, 2020),
            employees=int(max(20, revenue / random.uniform(200_000, 800_000))),
            annual_revenue_usd=float(revenue),
            description=f"{name} — {industry} supplier.",
        )
        db.add(c); db.flush()
        by_name[name] = c
        created += 1


def _profile_all(db: Session) -> None:
    print("[seed] Profiling companies and setting credit limits...")
    companies = db.query(models.Company).all()
    for c in companies:
        tool_build_risk_profile(db, company_id=c.id)
        tool_ensure_limits(db, company_id=c.id, product=PRODUCT_FACTORING)
        tool_ensure_limits(db, company_id=c.id, product=PRODUCT_REVERSE_FACTORING)
    db.commit()


def _build_programs(db: Session, buyer_pool: List[models.Company],
                    seller_pool: List[models.Company], target_count: int):
    programs: List[models.Program] = []
    seen = set()
    rng = random.Random(RNG_SEED + 1)
    while len(programs) < target_count:
        buyer = rng.choice(buyer_pool)
        seller = rng.choice(seller_pool)
        if buyer.id == seller.id:
            continue
        product = rng.choice([PRODUCT_FACTORING, PRODUCT_REVERSE_FACTORING])
        key = (buyer.id, seller.id, product)
        if key in seen:
            continue
        seen.add(key)
        buyer_cl = next((cl for cl in buyer.credit_limits if cl.product == product), None)
        seller_cl = next((cl for cl in seller.credit_limits if cl.product == product), None)
        if not buyer_cl or not seller_cl:
            continue
        limit = max(250_000.0, round(min(buyer_cl.limit_usd, seller_cl.limit_usd) * rng.uniform(0.05, 0.25), 2))
        prog = models.Program(
            name=f"{seller.name} → {buyer.name} ({product})",
            buyer_id=buyer.id, seller_id=seller.id, product=product,
            credit_limit_usd=limit,
            base_currency=rng.choice(["USD", "USD", "USD", "EUR", "CAD", "MXN", "BRL"]),
            grace_period_days=rng.choice([0, 3, 5, 7, 10]),
            status="ACTIVE",
        )
        db.add(prog)
        programs.append(prog)
    db.flush()
    return programs


def _seed_invoices(db: Session, programs: List[models.Program], n: int) -> None:
    rng = random.Random(RNG_SEED + 2)
    today = _dt.datetime.utcnow()
    two_years_ago = today - _dt.timedelta(days=730)
    rp_by = {rp.company_id: rp for rp in db.query(models.RiskProfile).all()}
    statuses = [
        ("FUNDED", 0.55), ("APPROVED", 0.10), ("PAID", 0.20),
        ("REVIEW", 0.05), ("UNDERWRITING", 0.04),
        ("REJECTED", 0.04), ("PENDING", 0.02),
    ]
    choices, weights = zip(*statuses)
    batch = []
    for i in range(n):
        prog = rng.choice(programs)
        currency = rng.choice(SUPPORTED_CURRENCIES)
        amount = round(rng.uniform(2_000, 750_000), 2)
        amount_usd = round(amount * FX_TO_USD[currency], 2)
        tenor = rng.choice([30, 60, 90])
        grace = rng.choice([0, 3, 5, 7])
        offset = rng.randint(0, 730)
        issue = two_years_ago + _dt.timedelta(days=offset, hours=rng.randint(0, 23))
        due = issue + _dt.timedelta(days=tenor)
        spread = (rp_by[prog.buyer_id].credit_spread + rp_by[prog.seller_id].credit_spread) / 2.0
        period = (tenor + grace) / 360.0
        fee = round(amount_usd * (BASE_RATE + spread) * period, 2)
        funded = round(amount_usd - fee, 2)
        status = rng.choices(choices, weights=weights, k=1)[0]
        reason = {
            "REJECTED": "Limit exceeded or risk threshold breached.",
            "UNDERWRITING": "Routed to underwriting (new pair).",
            "REVIEW": "Program limit exceeded; awaiting Review Agent.",
            "PAID": f"Buyer paid invoice on {due.date().isoformat()}.",
            "FUNDED": f"Funded ${funded:,.2f} at {(BASE_RATE+spread):.2%}.",
            "APPROVED": "Approved; awaiting funding.",
            "PENDING": "Pending evaluation.",
        }[status]
        if status in ("REJECTED", "UNDERWRITING"):
            fee = 0.0
            funded = 0.0
        inv = models.Invoice(
            invoice_number=f"INV-{i+1:08d}-{uuid.uuid4().hex[:6].upper()}",
            seller_id=prog.seller_id, buyer_id=prog.buyer_id, program_id=prog.id,
            product=prog.product, amount=amount, currency=currency, amount_usd=amount_usd,
            tenor_days=tenor, grace_period_days=grace,
            issue_date=issue, due_date=due,
            base_rate=BASE_RATE, credit_spread=round(spread, 4),
            fee_usd=fee, funded_amount_usd=funded, status=status, decision_reason=reason,
        )
        batch.append(inv)
        if status in ("FUNDED", "APPROVED"):
            prog.utilised_usd = round(prog.utilised_usd + amount_usd, 2)
            for cid in (prog.buyer_id, prog.seller_id):
                for cl in db.query(models.CreditLimit).filter(
                    models.CreditLimit.company_id == cid,
                    models.CreditLimit.product.in_(["GLOBAL", prog.product]),
                ).all():
                    cl.utilised_usd = round(cl.utilised_usd + amount_usd, 2)
        if len(batch) >= 1000:
            db.add_all(batch); db.flush()
            batch = []
            if (i+1) % 2000 == 0:
                print(f"  ...{i+1} invoices generated")
    if batch:
        db.add_all(batch); db.flush()
    db.commit()


def main(n_invoices: int = 12_000, n_sellers: int = 520) -> None:
    print("[seed] (Re)creating schema...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        by_name = _seed_companies(db); db.commit()
        _generate_extra_sellers(db, by_name, n_target=n_sellers); db.commit()
        _profile_all(db)

        sellers = db.query(models.Company).filter(models.Company.role.in_(["SELLER", "BOTH"])).all()
        all_buyers = db.query(models.Company).filter(models.Company.role.in_(["BUYER", "BOTH"])).all()
        leaf_buyers = [b for b in all_buyers if not b.children]
        buyer_pool = leaf_buyers if len(leaf_buyers) > 5 else all_buyers
        print(f"[seed] {len(sellers)} sellers, {len(buyer_pool)} buyer leaves.")
        programs = _build_programs(db, buyer_pool, sellers, target_count=min(2500, len(sellers) * 3))
        print(f"[seed] {len(programs)} programs.")
        db.commit()
        print(f"[seed] Generating {n_invoices} invoices...")
        _seed_invoices(db, programs, n_invoices)
        log_event(db, agent="orchestrator", action="PLATFORM_BOOT",
                  node="seed",
                  message=(f"Seeded with {db.query(models.Company).count()} companies, "
                           f"{db.query(models.Program).count()} programs, "
                           f"{db.query(models.Invoice).count()} invoices."))
        db.commit()
    finally:
        db.close()
    print("[seed] Complete.")


if __name__ == "__main__":
    n = 12_000
    if len(sys.argv) > 1:
        try:
            n = int(sys.argv[1])
        except ValueError:
            pass
    main(n_invoices=n)
