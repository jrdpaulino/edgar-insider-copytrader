"""Offline tests for the EDGAR collector. Run with: python -m unittest -v"""
import unittest
from datetime import date

from edgar import EdgarClient, EdgarError, collect, daily_index_url, parse_form4, parse_index
from state import State

FILED = date(2026, 11, 13)


def tx_xml(code="P", shares="10000", price="12.50", ad="A", day="2026-11-10", swap="0", notes=""):
    return f"""<nonDerivativeTransaction>
<securityTitle><value>Common Stock</value></securityTitle>
<transactionDate><value>{day}</value></transactionDate>
<transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode><equitySwapInvolved>{swap}</equitySwapInvolved></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares><transactionPricePerShare><value>{price}</value>{notes}</transactionPricePerShare><transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>110000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
<ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
</nonDerivativeTransaction>"""


def owner_xml(cik="0000999001", name="Doe Jane", officer=1, director=0, ten=0, title="Chief Executive Officer"):
    return f"""<reportingOwner><reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>{director}</isDirector><isOfficer>{officer}</isOfficer><isTenPercentOwner>{ten}</isTenPercentOwner><isOther>0</isOther><officerTitle>{title}</officerTitle></reportingOwnerRelationship></reportingOwner>"""


def form4(txs, owners=None, symbol="ACME", plan="0", doc="4", footnotes=""):
    owners = owners or [owner_xml()]
    return f"""<SEC-DOCUMENT>0000123456-26-000010.txt
<DOCUMENT><TYPE>4
<XML>
<?xml version="1.0"?>
<ownershipDocument><schemaVersion>X0508</schemaVersion><documentType>{doc}</documentType><periodOfReport>2026-11-10</periodOfReport><aff10b5One>{plan}</aff10b5One>
<issuer><issuerCik>0000123456</issuerCik><issuerName>ACME CORP</issuerName><issuerTradingSymbol>{symbol}</issuerTradingSymbol></issuer>
{''.join(owners)}
<nonDerivativeTable>{''.join(txs)}</nonDerivativeTable>
{footnotes}
</ownershipDocument>
</XML>
</DOCUMENT></SEC-DOCUMENT>"""


INDEX = """Description:           Daily Form Index
Last Data Received:    November 13, 2026

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------
4           ACME CORP                                                     123456      20261113    edgar/data/123456/0000123456-26-000010.txt
4           DOE JANE                                                      999001      20261113    edgar/data/999001/0000123456-26-000010.txt
4/A         AMENDED CO                                                    555         20261113    edgar/data/555/0000000555-26-000001.txt
8-K         SOME OTHER CO                                                 777         20261113    edgar/data/777/0000000777-26-000002.txt
4           BETA INC                                                      222222      20261113    edgar/data/222222/0000222222-26-000020.txt
"""


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        r = self.routes.get(url)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        return r or FakeResp(404)


def client(routes):
    sess = FakeSession(routes)
    return EdgarClient("Test User test@example.com", delay=0, session=sess, sleep=lambda s: None), sess


class ParserTests(unittest.TestCase):
    def test_parses_open_market_purchase(self):
        rows = parse_form4("acc-1", FILED, form4([tx_xml()]))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["symbol"], r["owner_name"], r["title"]), ("ACME", "Doe Jane", "Chief Executive Officer"))
        self.assertEqual((r["shares"], r["price"], r["value"]), (10000.0, 12.5, 125000.0))
        self.assertEqual((r["is_officer"], r["is_director"], r["tenb5"]), (1, 0, 0))
        self.assertEqual((r["tx_date"], r["filed"]), ("2026-11-10", "2026-11-13"))

    def test_ignores_everything_but_acquired_purchases(self):
        txs = [tx_xml("S", ad="D"), tx_xml("A"), tx_xml("M"), tx_xml("F", ad="D"), tx_xml("G", ad="D"),
               tx_xml("P", ad="D"), tx_xml("P", swap="1")]
        self.assertEqual(parse_form4("a", FILED, form4(txs)), [])

    def test_plan_flag_marks_rows(self):
        rows = parse_form4("a", FILED, form4([tx_xml()], plan="1"))
        self.assertEqual(rows[0]["tenb5"], 1)

    def test_plan_footnote_applies_only_to_its_transaction(self):
        fn = ('<footnotes><footnote id="F1">Purchased pursuant to a Rule 10b5-1 trading plan.</footnote>'
              '</footnotes>')
        txs = [tx_xml(notes='<footnoteId id="F1"/>'), tx_xml(shares="500")]
        rows = parse_form4("a", FILED, form4(txs, footnotes=fn))
        self.assertEqual([r["tenb5"] for r in rows], [1, 0])

    def test_prefers_officer_or_director_among_joint_owners(self):
        owners = [owner_xml("1", "Big Fund LP", officer=0, ten=1, title=""),
                  owner_xml("2", "Roe Rick", officer=0, director=1, title="")]
        r = parse_form4("a", FILED, form4([tx_xml()], owners=owners))[0]
        self.assertEqual((r["owner_name"], r["is_director"], r["is_ten_pct"]), ("Roe Rick", 1, 0))

    def test_unparseable_and_amendments(self):
        self.assertIsNone(parse_form4("a", FILED, "no xml here"))
        self.assertIsNone(parse_form4("a", FILED, "<ownershipDocument><broken></ownershipDocument>"))
        self.assertEqual(parse_form4("a", FILED, form4([tx_xml()], doc="4/A")), [])

    def test_bad_numbers_skipped(self):
        txs = [tx_xml(price=""), tx_xml(shares="0"), tx_xml(day="not-a-date")]
        self.assertEqual(parse_form4("a", FILED, form4(txs)), [])


class IndexTests(unittest.TestCase):
    def test_parse_index_dedupes_and_filters(self):
        entries = parse_index(INDEX)
        self.assertEqual([e.accession for e in entries], ["0000123456-26-000010", "0000222222-26-000020"])
        self.assertEqual(entries[0].filed, FILED)
        self.assertEqual(entries[0].path, "edgar/data/123456/0000123456-26-000010.txt")

    def test_index_url(self):
        self.assertEqual(daily_index_url(FILED),
                         "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261113.idx")


class ClientTests(unittest.TestCase):
    def test_requires_contact_email(self):
        with self.assertRaises(SystemExit):
            EdgarClient("just a name")

    def test_missing_index_is_none_for_403_and_404(self):
        c, _ = client({daily_index_url(FILED): FakeResp(403)})
        self.assertIsNone(c.get_index(FILED))
        self.assertIsNone(c.get_index(date(2026, 11, 12)))   # unrouted -> 404

    def test_rate_limit_retries_then_raises(self):
        url = "https://www.sec.gov/Archives/edgar/data/1/x.txt"
        c, sess = client({url: FakeResp(429)})
        with self.assertRaises(EdgarError):
            c.get_filing("edgar/data/1/x.txt")
        self.assertEqual(len(sess.calls), 3)

    def test_retry_then_success(self):
        url = "https://www.sec.gov/Archives/edgar/data/1/x.txt"
        c, _ = client({url: [FakeResp(503), FakeResp(200, "ok")]})
        self.assertEqual(c.get_filing("edgar/data/1/x.txt"), "ok")


class CollectTests(unittest.TestCase):
    def routes(self):
        base = "https://www.sec.gov/Archives/"
        return {
            daily_index_url(FILED): FakeResp(200, INDEX),
            base + "edgar/data/123456/0000123456-26-000010.txt": FakeResp(200, form4([tx_xml()])),
            base + "edgar/data/222222/0000222222-26-000020.txt": FakeResp(200, form4([tx_xml("S", ad="D")])),
        }

    def test_collects_once_and_skips_known_filings(self):
        c, sess = client(self.routes())
        st = State(":memory:")
        stats = collect(c, st, FILED, days=0)
        self.assertEqual((stats["indexes"], stats["new_filings"], stats["purchases"]), (1, 2, 1))
        self.assertEqual(len(st.buys_since(date(2026, 11, 1))), 1)
        calls_after_first = len(sess.calls)
        collect(c, st, FILED, days=0)
        self.assertEqual(len(sess.calls) - calls_after_first, 1)   # only the index is fetched again

    def test_weekends_skipped(self):
        c, sess = client({})
        stats = collect(c, State(":memory:"), date(2026, 11, 15), days=1)   # Sat + Sun
        self.assertEqual((stats["indexes"], sess.calls), (0, []))

    def test_unparseable_filing_recorded(self):
        routes = self.routes()
        routes["https://www.sec.gov/Archives/edgar/data/222222/0000222222-26-000020.txt"] = FakeResp(200, "garbage")
        c, _ = client(routes)
        st = State(":memory:")
        stats = collect(c, st, FILED, days=0)
        self.assertEqual(stats["unparseable"], 1)
        self.assertTrue(st.filing_known("0000222222-26-000020"))

    def test_too_many_failures_aborts(self):
        c, _ = client({daily_index_url(FILED): FakeResp(200, INDEX)})   # filings all 404
        with self.assertRaises(EdgarError):
            collect(c, State(":memory:"), FILED, days=0, max_failures=1)


if __name__ == "__main__":
    unittest.main()
