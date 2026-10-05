// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "OKXSelfTrader",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "mac-trader", targets: ["MacTraderApp"])
    ],
    dependencies: [
        .package(url: "https://github.com/hummingbird-project/hummingbird.git", branch: "1.x.x"),
        .package(url: "https://github.com/hummingbird-project/hummingbird-websocket.git", branch: "1.x.x")
    ],
    targets: [
        .target(name: "TradingDomain"),
        .target(name: "OKXGateway", dependencies: ["TradingDomain"]),
        .target(name: "ATKGateway", dependencies: ["TradingDomain"]),
        .target(name: "TradingService", dependencies: [
            "TradingDomain", "OKXGateway", "ATKGateway",
            .product(name: "Hummingbird", package: "hummingbird"),
            .product(name: "HummingbirdFoundation", package: "hummingbird"),
            .product(name: "HummingbirdWebSocket", package: "hummingbird-websocket")
        ]),
        .target(name: "TradingServiceClient", dependencies: ["TradingDomain"]),
        .executableTarget(
            name: "MacTraderApp",
            dependencies: ["TradingDomain", "TradingServiceClient"],
            exclude: ["Resources"]
        ),
        .testTarget(name: "OKXGatewayTests", dependencies: ["OKXGateway", "ATKGateway", "TradingDomain", "TradingService"])
    ]
)
