# Trading insight from LP range choices

## What LPs did

From 2026-07-18 through 2026-09-15, the pool recorded 69,779 Swaps and 6,263 Mints. ETH moved from 1,860.06 to 2,405.14 USDT per ETH, a simple return of 29.3%.

LPs placed 88.6% of Mint value in ranges that contained the current price. Only 4.6% was entirely above and 6.7% entirely below. The dominant behavior was therefore active market making near price, not one-sided directional positioning.

## Directional insight

On the 35 below-tilted days, the next-day mean log return was 0.0083, the median was 0.0057, and 60.0% of valid next days were positive. On the 22 above-tilted days, the corresponding values were -0.0035, -0.0008, and 47.6%. Three days had no above-versus-below imbalance and are kept neutral.

That sign split looks potentially useful, but it is not a stable ranked signal. Equal-frequency below, middle, and above groups produced mean next-day log returns of -0.0043, 0.0199, and -0.0030. The middle group performed best, and the overall imbalance/next-day-return correlation was only 0.029. The data do not show that progressively more above- or below-market liquidity leads to progressively different returns.

## Volatility insight

Narrow, middle, and wide range-selection groups were followed by mean next-day log high-low ranges of 0.0286, 0.0352, and 0.0307. The width/next-day-range correlation was -0.009. Wider LP ranges did not precede greater next-day price variation in this sample.

## Practical reading

Above-market liquidity behaves like potential ETH supply as price rises through it; below-market liquidity behaves like potential ETH demand as price falls through it. This makes LP tilt useful as market context. Here, however, the strongest result is negative: aggregate Mint placement and selected width were weak standalone forecasts. A trader could monitor unusually one-sided positioning as confirmation for another view, but these data do not support using LP range choices alone as an entry signal.
