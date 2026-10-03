import Foundation

/// Runtime copy of the strategy-lab universe boundary.
///
/// The research universe files are intentionally not loaded by the app. Keep
/// this small, explicit eligibility rule in the compiled domain model and
/// update it from `strategies/sweep_reversal_short/config/universe.json` when
/// the lab changes its asset-class exclusions.
public enum StrategyUniverseRules {
    /// Strategy-specific production guardrails. They are applied after the
    /// common altcoin asset-class filter; none of these pools are a generic
    /// top-20 fallback.
    /// sweep_reversal_short v1.4 `runtime_universe`: drop alts below the
    /// quote-volume floor, then keep the top `limit` by 24h quote volume.
    public static let sweepCandidateLimit = 100
    public static let sweepMinimumQuoteVolume24h: Decimal = 3_000_000
    public static let doublePumpMinimumGainPercent: Decimal = 100
    public static let doublePumpMinimumQuoteVolume24h: Decimal = 10_000_000
    public static let mainstreamSymbols: Set<String> = [
        "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
        "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI"
    ]

    /// Stablecoins and non-crypto bases present in the exchange SWAP ticker
    /// feed. This mirrors the lab's `universe.json` exclusion snapshot.
    public static let excludedNonCryptoSymbols: Set<String> = [
        "AAOI", "AAPL", "ADBE", "AEHR", "ALAB", "AMAT", "AMC", "AMD", "AMZN", "ANTHROPIC",
        "APLD", "APP", "ARM", "ASML", "ASTS", "AVGO", "AXTI", "BB", "BE", "BRKB", "BTC",
        "BUSD", "BX", "BZ", "CGNX", "CIEN", "CL", "COHR", "COIN", "COST", "CRCL", "CRDO",
        "CRM", "CRWD", "CRWV", "CSCO", "CSOPSAMSUNG2L", "CSOPSKHYNIX2L", "CXMT", "DAI", "DDOG",
        "DELL", "DKNG", "EURS", "EURT", "EWJ", "EWT", "EWY", "EWZ", "FDUSD", "FLNC", "FLY",
        "GEV", "GLW", "GME", "GOOGL", "GTLB", "GUSD", "HANMI", "HIMS", "HOOD", "HPE", "HUT",
        "HYUNDAI", "IBM", "INTC", "INTW", "IONQ", "IREN", "ISRG", "IWM", "JNJ", "JP225", "KIOXIA",
        "KLAC", "KO", "KORU", "KR200", "KSTR", "LGELECTRONICS", "LITE", "LLY", "LRCX", "LUNR",
        "LYTE", "MARA", "META", "MINIMAX", "MOONSHOT", "MRK", "MRNA", "MRVL", "MSFT", "MSTR", "MSTU",
        "MU", "MUU", "MVLL", "NAVER", "NBIS", "NET", "NFLX", "NG", "NOK", "NOW", "NVDA", "NVDL",
        "OKLO", "OKTA", "ON", "ONDS", "OPENAI", "ORCL", "OSCR", "OURA", "OUST", "PLTR", "POET",
        "POPMART", "PYPL", "PYUSD", "QCOM", "QQQ", "RDDT", "RDW", "RIOT", "RIVN", "RKLB", "ROK",
        "SAMSUNG", "SHAZ", "SHEIN", "SHLD", "SHOP", "SIMO", "SKDD", "SKHY", "SKHYNIX", "SKUU", "SMCI",
        "SMH", "SNDK", "SNOW", "SOFTBANK", "SONY", "SOXL", "SOXS", "SPCH", "SPCX", "SPY", "SQQQ",
        "STABLE", "SUSD", "TEAM", "TEM", "TER", "TMF", "TQQQ", "TSEM", "TSLA", "TSLL", "TSM", "TTMI",
        "TTWO", "TUSD", "TWLO", "UNH", "UNITREE", "URNM", "US100", "US500", "USD1", "USDC", "USDE",
        "USDF", "USDP", "USDT", "USDY", "USO", "UVXY", "VRT", "WDC", "WEN", "WMT", "XAG", "XAU",
        "XBI", "XCU", "XIAOMI", "XLE", "XOM", "XPD", "XPT", "ZHIPU", "ZHONGJI", "ZM"
    ]

    public static func isEligibleHotAltcoin(_ contract: ContractMarket) -> Bool {
        return isEligibleHotAltcoin(instrumentID: contract.id, baseCurrency: contract.baseCurrency, quoteCurrency: contract.quoteCurrency)
    }

    public static func isEligibleHotAltcoin(instrumentID: String, baseCurrency: String, quoteCurrency: String) -> Bool {
        let base = baseCurrency.uppercased()
        return instrumentID.uppercased().hasSuffix("-USDT-SWAP")
            && quoteCurrency.uppercased() == "USDT"
            && !base.hasSuffix("USD")
            && !mainstreamSymbols.contains(base)
            && !excludedNonCryptoSymbols.contains(base)
    }

    public static func isEligibleSweep(_ contract: ContractMarket) -> Bool {
        isEligibleHotAltcoin(contract)
            && contract.volume24h >= sweepMinimumQuoteVolume24h
    }

    public static func isEligibleDoublePump(_ contract: ContractMarket) -> Bool {
        isEligibleHotAltcoin(contract)
            && contract.rollingChangePercent > doublePumpMinimumGainPercent
            && contract.volume24h >= doublePumpMinimumQuoteVolume24h
    }

}
