from dhan.instruments import parse_scrip_master_detailed


def test_detailed_master_preserves_option_contract_metadata_and_birth_date():
    csv_text = """EXCH_ID,SEGMENT,INSTRUMENT,INSTRUMENT_TYPE,SECURITY_ID,OPTION_TYPE,STRIKE_PRICE,SM_EXPIRY_DATE,LOT_SIZE,UNDERLYING_SYMBOL,UNDERLYING_SECURITY_ID,TICK_SIZE,DISPLAY_NAME,EXPIRY_FLAG,CONTRACT_BIRTH_DATE,STRIKE_INTERVAL
NSE,D,OPTSTK,OP,700001,CE,1500,2026-09-29,250,RELIANCE,2885,5.0000,RELIANCE 1500 CE,M,2026-09-01,50
NSE,D,OPTSTK,OP,700002,PE,1500,2026-09-29,250,RELIANCE,2885,0.05,RELIANCE 1500 PE,M,,50
"""
    bundle = parse_scrip_master_detailed(csv_text)
    by_id = {c.security_id: c for c in bundle.contracts}

    call = by_id["700001"]
    assert call.instrument_token == "700001"
    assert call.exchange == "NSE"
    assert call.exchange_segment == "NSE_FNO"
    assert call.tick_size == 0.05
    assert call.contract_birth_date == "2026-09-01"
    assert call.expiry_flag == "M"
    assert call.strike_interval == 50
    assert call.lot_size == 250

    put = by_id["700002"]
    assert put.contract_birth_date is None
    assert bundle.tick_sizes["700002"] == 0.05


def test_detailed_master_does_not_replace_missing_option_birth_with_underlying_date():
    csv_text = """EXCH_ID,INSTRUMENT_TYPE,SECURITY_ID,OPTION_TYPE,STRIKE_PRICE,SM_EXPIRY_DATE,LOT_SIZE,UNDERLYING_SYMBOL,UNDERLYING_SECURITY_ID
NSE,OP,700003,CE,1500,2026-09-29,250,RELIANCE,2885
"""
    contract = parse_scrip_master_detailed(csv_text).contracts[0]
    assert contract.contract_birth_date is None
