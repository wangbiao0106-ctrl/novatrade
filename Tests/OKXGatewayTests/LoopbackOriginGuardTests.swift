import Testing
import TradingService

@Test
func loopbackGuardAllowsNativeClientsWithoutOrigin() {
    #expect(LoopbackOriginGuardMiddleware.isAllowed(host: "127.0.0.1:8787", origin: nil))
    #expect(LoopbackOriginGuardMiddleware.isAllowed(host: "localhost:8787", origin: nil))
    #expect(LoopbackOriginGuardMiddleware.isAllowed(host: "[::1]:8787", origin: nil))
}

@Test
func loopbackGuardRejectsCrossSiteBrowserRequests() {
    #expect(!LoopbackOriginGuardMiddleware.isAllowed(host: "127.0.0.1:8787", origin: "https://evil.example"))
    #expect(!LoopbackOriginGuardMiddleware.isAllowed(host: "127.0.0.1:8787", origin: "null"))
    #expect(!LoopbackOriginGuardMiddleware.isAllowed(host: "127.0.0.1:8787", origin: "http://127.0.0.1:3000"))
    #expect(LoopbackOriginGuardMiddleware.isAllowed(host: "127.0.0.1:8787", origin: "http://127.0.0.1:8787"))
}

@Test
func loopbackGuardRejectsDNSRebindingAndMissingHost() {
    #expect(!LoopbackOriginGuardMiddleware.isAllowed(host: "rebind.evil.example:8787", origin: nil))
    #expect(!LoopbackOriginGuardMiddleware.isAllowed(host: "127.0.0.1.evil.example", origin: nil))
    #expect(!LoopbackOriginGuardMiddleware.isAllowed(host: nil, origin: nil))
}
