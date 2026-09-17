# Trading Insight from Uniswap v3 Liquidity Ranges

## Concentrated liquidity

In a traditional constant-product automated market maker, reserves satisfy

```text
x * y = k
```

and liquidity is available across every possible price. Uniswap v3 allows each liquidity provider to choose a lower and upper price, so the provider earns fees and holds inventory only while the market trades within that interval. This is concentrated liquidity: the same capital can provide more liquidity near the current price, but it stops earning fees when price leaves the chosen range.

Let `P` be USDT per ETH, `Pa` and `Pb` the lower and upper range prices, and `L` the position's liquidity. Ignoring token decimal adjustments, the token amounts follow:

```text
If P <= Pa:
    WETH = L * (1/sqrt(Pa) - 1/sqrt(Pb))
    USDT = 0

If Pa < P < Pb:
    WETH = L * (1/sqrt(P) - 1/sqrt(Pb))
    USDT = L * (sqrt(P) - sqrt(Pa))

If P >= Pb:
    WETH = 0
    USDT = L * (sqrt(Pb) - sqrt(Pa))
```

The position therefore changes inventory as price moves through its range. A range entirely above the current market begins as WETH and gradually sells WETH for USDT if price rises. It can be interpreted as potential sell-side supply, not automatically as a bullish view. A range entirely below the market begins as USDT and gradually buys WETH if price falls, behaving more like potential buy-side demand.

Uniswap stores price using square-root price and ticks. For this WETH/USDT pool:

```text
current_price = (sqrt_price_x96 / 2**96)**2 * 10**12
range_price   = 1.0001**tick * 10**12
```

The decimal factor converts WETH's 18 decimals and USDT's 6 decimals into USDT per ETH. For each Mint, this project measures:

```text
range_center = sqrt(lower_price * upper_price)
displacement = log(range_center / current_price)
range_width  = log(upper_price / lower_price)
LP imbalance = above_value_share - below_value_share
```

Positive imbalance means more newly added value was placed entirely above price; negative imbalance means more was placed entirely below. Mint events describe additions, not the pool's complete outstanding liquidity distribution.

## Evidence from the 60-day sample

The sample covers July 18 through September 15, 2026 and contains 69,779 Swaps and 6,263 Mints. ETH's pool closing price rose from 1,860.06 to 2,405.14 USDT per ETH over the period.

LP behavior was primarily market making rather than directional positioning:

- 88.63% of Mint value was placed in ranges containing the current price.
- 6.75% was entirely below the current price.
- 4.63% was entirely above the current price.
- Total estimated Mint value was 431.66 million USDT.

The first directional comparison looks interesting. On 35 days with more Mint value below than above price, the next-day mean log return was 0.0083, the median was 0.0057, and 60.0% of valid next days were positive. On 22 days with more value above price, the next-day mean was -0.0035, the median was -0.0008, and 47.6% were positive. Three days had no above-versus-below imbalance and are treated as neutral.

That split is not strong enough to treat as a trading signal. When imbalance is divided into equal-frequency below-tilted, middle, and above-tilted groups, their mean next-day log returns are -0.0043, 0.0199, and -0.0030. The middle group, not either directional extreme, has the highest return. The overall correlation between LP imbalance and next-day return is only 0.029. Average range-center displacement has a -0.059 correlation with next-day return. There is no monotonic directional pattern.

Selected range width also provides little forward-looking volatility information. Narrow, middle, and wide groups are followed by average next-day log high-low ranges of 0.0286, 0.0352, and 0.0307. The width/next-day-range correlation is -0.009. Wider selected ranges did not consistently precede greater next-day variation.

## Trading interpretation

The useful insight is contextual rather than predictive. Above-market additions identify potential ETH supply if price rises, while below-market additions identify potential demand if price falls. A strongly one-sided day may help describe where LPs are willing to rebalance inventory and can be monitored alongside price, volume, and other market evidence.

In this sample, however, aggregate Mint placement is a weak standalone forecast. Most capital stays around the market, the directional relationship is not ranked or monotonic, and selected width does not forecast the next day's high-low range. LP positioning may still be useful as confirmation for an independently formed trading view, but the evidence does not support using it alone for entry or risk sizing.

These results are descriptive. Mints show new additions rather than total outstanding liquidity; LP actions may react to price changes already underway; aggregate positioning does not reveal individual beliefs; and correlation does not establish causation or a reliable trading strategy.
