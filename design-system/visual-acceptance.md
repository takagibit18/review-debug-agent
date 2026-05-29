# Visual Acceptance

This document defines pass/fail checks for the MergeWarden marketing page visual system. It is not a page implementation plan.

## Header

- Pass if the header is fixed and remains detached from page flow while scrolling.
- Pass if desktop layout has left nav, centered mark, and right CTA.
- Pass if the header has no solid rectangular background.
- Pass if the CTA height is `44-46px` and uses a `6px` radius.
- Fail if the header looks like a standard SaaS navbar block.
- Fail if the logo is visually lost against the background.

## Hero

- Pass if the H1 visually dominates the viewport.
- Pass if the H1 occupies `65-75%` of desktop viewport width (approximation).
- Pass if the H1 starts around `34-38vh` on desktop (approximation).
- Pass if the background glow sits behind the H1.
- Pass if the page still reads with background texture opacity at or below `0.25`.
- Fail if the hero becomes a split text/image layout.
- Fail if the H1 is reduced to ordinary landing-page scale.
- Fail if glow is placed only at the top or bottom instead of behind the title.

## MissionSection

- Pass if the section has label, vertical line, and large centered title only.
- Pass if section height is at least `90vh`.
- Pass if large empty space remains above and below the title.
- Pass if the label and line are visibly smaller than the title.
- Fail if cards, statistics, screenshots, or decorative blocks are added.
- Fail if section spacing is compact.

## ProblemCardsSection

- Pass if desktop shows two full oversized cards plus part of a third card.
- Pass if cards use horizontal overflow on desktop.
- Pass if section height is at least `100vh`.
- Pass if title appears before the card track with `90-120px` spacing (approximation).
- Fail if cards are squeezed into a narrow centered container.
- Fail if all cards become small equal SaaS feature tiles on desktop.
- Fail if card backgrounds become flat blue panels.

## ProblemCard

- Pass if each card has dark glass background, blue border, low-opacity technical texture, icon, title, and body.
- Pass if card radius is `16px`.
- Pass if border opacity stays in the `0.22-0.45` range by default.
- Pass if text remains readable when texture is present.
- Pass if hover only lifts by about `4px` and brightens border/glow.
- Fail if the card uses heavy black shadow.
- Fail if card radius is `24px` or larger.
- Fail if texture competes with text.

## SolutionDiagram

- Pass if the diagram reads as an infrastructure architecture flow.
- Pass if desktop flow is horizontal.
- Pass if mobile flow becomes vertical.
- Pass if connector lines are thin, `1-1.5px` (approximation).
- Pass if MergeWarden guard nodes are visually dominant.
- Pass if peripheral nodes are compact and quieter than the guard nodes.
- Fail if it looks like a generic marketing infographic.
- Fail if lines are thick, colorful, or chart-like.
- Fail if a chart library visual style is visible.

## ValueCard

- Pass if desktop layout uses 3 cards across.
- Pass if tablet layout uses 2 columns.
- Pass if mobile layout uses 1 column.
- Pass if the card style matches ProblemCard but can be slightly shorter.
- Pass if each card uses one icon, one title, and one body block.
- Fail if cards are nested inside another card container.
- Fail if value cards use a different color system from ProblemCard.

## FooterCTA

- Pass if the footer occupies at least `100vh`.
- Pass if CTA, links, copyright, and giant wordmark are vertically distinct.
- Pass if the giant wordmark is the main footer visual anchor.
- Pass if the wordmark uses uppercase, heavy weight, compressed line-height, and negative tracking.
- Pass if the background glow grows stronger toward the lower area.
- Fail if the footer becomes a conventional dense footer.
- Fail if the wordmark is small or secondary.
- Fail if link blocks dominate the footer.

## GlowBackground

- Pass if every major section uses base color, radial glow, texture, and vignette layers.
- Pass if Hero glow is the strongest background glow.
- Pass if Mission glow is restrained.
- Pass if texture opacity stays within `0.12-0.25`.
- Pass if all texture is blue/white and low contrast.
- Fail if the background uses purple, pink, green, yellow, or rainbow gradients.
- Fail if colorful screenshots are used as code texture.
- Fail if text readability drops below acceptable contrast.

## Do Not

- Do not implement a generic SaaS gradient background.
- Do not use purple, pink, green, yellow, or rainbow gradients.
- Do not use thick card shadows.
- Do not use rounded `24px+` soft startup cards.
- Do not use emoji icons.
- Do not use colorful 3D illustrations.
- Do not use stock photos.
- Do not make the navbar a solid rectangle.
- Do not center every card into a narrow container.
- Do not reduce the hero title below the token scale.
- Do not make section spacing compact.
- Do not animate every element.
- Do not use heavy frosted-glass blur.
- Do not copy proprietary SVGs from the reference site.
- Do not introduce browser-visible developer IDs, logs, or internal run metadata into the primary marketing UI.

## Implementation Priority

1. Priority 1:
   - Page architecture.
   - Typography scale.
   - Dark background.
   - Section min-heights.
   - Header positioning.
   - Token usage.

2. Priority 2:
   - Oversized problem cards.
   - Solution diagram structure.
   - Footer giant wordmark.
   - Blue radial glow placement.

3. Priority 3:
   - Code/data texture.
   - Hover effects.
   - Scroll reveal.
   - Low-speed parallax.

4. Priority 4:
   - Diagram stroke drawing.
   - Fine-grained stagger.
   - Micro-interactions.
   - Section-specific motion tuning.

## Token Compliance

- Pass if all major colors come from `design-tokens.ts`.
- Pass if all heading, body, label, nav, and button typography references `design-tokens.ts`.
- Pass if component radius values reference `design-tokens.ts`.
- Pass if motion duration and easing values reference `design-tokens.ts`.
- Fail if a component hardcodes primary colors, font sizes, radius, or section spacing without a local exception comment.
