# Component Spec

This file defines the MergeWarden marketing page component behavior. It is a design-system contract only. Do not add page implementation code here.

All components must import major values from `design-tokens.ts`. Do not hardcode primary colors, font sizes, section spacing, radius, borders, or motion values unless the value is marked as a local exception.

## Header

- Position: fixed.
- Offset: `top: tokens.layout.headerTop`; `left/right: tokens.layout.pagePaddingDesktop`.
- Mobile offset: `left/right: tokens.layout.pagePaddingMobile`.
- Z index: `tokens.layout.headerZIndex`.
- Layout desktop: 3 columns.
  - Left: `-> About` link.
  - Center: MergeWarden wordmark or compact mark.
  - Right: primary light CTA.
- Layout mobile:
  - Left: logo.
  - Right: CTA.
  - Hide `About` or move it into a menu.
- Header background: transparent.
- Do not add a solid navbar rectangle.
- Do not add heavy backdrop blur.
- Navigation font: `tokens.typography.nav`.
- CTA button:
  - Height: `46px`.
  - Padding: `0 20px`.
  - Radius: `tokens.radius.button`.
  - Background: `tokens.colors.white`.
  - Text: `tokens.colors.textInverse`.
- Hover:
  - Link opacity moves from `0.82` to `1`.
  - Arrow translates `4px` right.
  - Duration: `tokens.motion.durationFast`.

## Hero

- Section min height: `tokens.layout.sectionMinHeightHero`.
- Content alignment: centered.
- H1 vertical position desktop: `tokens.layout.heroTitleTopDesktop`.
- H1 vertical position mobile: `tokens.layout.heroTitleTopMobile`.
- H1 width desktop: `65-75vw`; default `tokens.layout.heroTitleWidthDesktop` (approximation).
- H1 typography: `tokens.typography.hero`.
- Desktop line count target: 3 lines.
- Mobile line count target: 4-5 lines.
- Background intensity: strongest glow on the page.
- Glow position: behind the H1, not at the top or bottom of the viewport.
- Optional lower-right status cue:
  - Font: mono.
  - Color: `tokens.colors.textPrimary`.
  - Size: `16-18px` (approximation).
  - Place within bottom-right safe area; keep at least `52px` from viewport edges.
- Do not use split hero layout.
- Do not use generic product screenshot in the hero.

## SectionLabel

- Shape: pill.
- Height: `42px`.
- Padding: `0 18px`.
- Radius: `tokens.radius.pill`.
- Border: `tokens.border.strong`.
- Background: `rgba(5,10,30,0.35)`.
- Text color: `tokens.colors.bluePrimary`.
- Typography: `tokens.typography.label`.
- Center aligned by default.
- Optional vertical line:
  - Width: `1px`.
  - Height: `38-48px` (approximation).
  - Color: `tokens.colors.blueLine`.
  - Top margin: `tokens.spacing.labelToLine`.
- Label must stay visually smaller than the section title.

## MissionSection

- Section min height: `tokens.layout.sectionMinHeightMission`.
- Content stack: `SectionLabel` -> vertical line -> large centered title.
- Top/bottom padding: `tokens.spacing.sectionY`.
- Title typography: `tokens.typography.sectionTitle`.
- Title max width: `tokens.layout.maxSectionTitle`.
- Title alignment: center.
- Background:
  - Base: `tokens.colors.bgBase`.
  - Minimal section glow: use `tokens.glow.sectionRadial` at opacity `0.2-0.35` (approximation).
  - Vignette allowed.
- No cards.
- No screenshots.
- No decorative statistics blocks.
- Preserve negative space; do not fill gaps with extra copy.

## ProblemCardsSection

- Section min height: `tokens.layout.sectionMinHeightProblem`.
- Content stack: `SectionLabel` or section title -> oversized horizontal card track.
- Title typography: `tokens.typography.sectionTitle`.
- Title-to-cards spacing: `tokens.spacing.titleToCards`.
- Desktop track:
  - Horizontal layout.
  - Gap: `tokens.spacing.cardGap`.
  - Show 2 full cards plus a partial third card.
  - Allow horizontal overflow; do not squeeze into a narrow centered grid.
- Tablet:
  - 2-column grid.
  - No partial card requirement.
- Mobile:
  - 1-column stack.
  - Full-width cards.
  - No horizontal overflow.
- Background texture opacity: `0.12-0.25`.

## ProblemCard

- Desktop width: `tokens.layout.cardWidthDesktop` (approximation).
- Desktop height: `tokens.layout.cardHeightDesktop` (approximation).
- Mobile height: content-driven, minimum `360px` (approximation).
- Padding desktop: `44px`.
- Padding mobile: `28px`.
- Radius: `tokens.radius.card`.
- Border: `tokens.border.strong`.
- Background: `linear-gradient(180deg, tokens.colors.bgPanel, tokens.colors.bgPanelDeep)`.
- Inner glow: `tokens.glow.cardInner`.
- Icon:
  - Size: `28-34px` (approximation).
  - Stroke: `1.25-1.5px`.
  - Color: `tokens.colors.bluePrimary`.
- Title:
  - Typography: `tokens.typography.cardTitle`.
  - Color: `tokens.colors.bluePrimary`.
  - Top margin after icon: `32px` (approximation).
- Body:
  - Typography: `tokens.typography.cardBody`.
  - Color: `tokens.colors.textPrimary`.
  - Max width: `460px` (approximation).
- Texture:
  - Right or center-right aligned.
  - Opacity: `0.12-0.25`.
  - Must not reduce text contrast.
- Hover desktop:
  - Border changes to `tokens.border.bright`.
  - Shadow changes to `tokens.glow.cardHover`.
  - Transform: `translateY(-4px)`.
  - Duration: `tokens.motion.durationFast`.

## SolutionDiagram

- Section min height: `tokens.layout.sectionMinHeightSolution`.
- Content stack: title -> subtitle -> diagram -> value card row.
- Title typography: `tokens.typography.sectionTitle`.
- Subtitle:
  - Max width: `720px` (approximation).
  - Color: `tokens.colors.textSecondary`.
  - Font size: `20-24px` (approximation).
  - Line height: `1.35`.
- Diagram desktop:
  - Horizontal architecture flow.
  - Left inputs -> MergeWarden guard -> model/review analysis -> MergeWarden guard -> outputs.
  - Use SVG or positioned div nodes.
  - Avoid chart libraries.
- Diagram mobile:
  - Vertical pipeline: Input -> MergeWarden -> Review / Analysis -> MergeWarden -> Output.
- Nodes:
  - Compact rectangles or pills.
  - Border: `tokens.border.node`.
  - Radius: `tokens.radius.node` or `tokens.radius.diagramPill`.
  - Background: `rgba(8,14,38,0.82)`.
  - Icon color: `tokens.colors.bluePrimary`.
- Lines:
  - Width: `1-1.5px` (approximation).
  - Color: `rgba(100,140,220,0.55)`.
  - Curved connector lines allowed.
- MergeWarden node:
  - Must be larger or brighter than peripheral nodes.
  - Add `tokens.glow.nodeGlow`.
- Motion:
  - Draw lines after title appears.
  - Node stagger: `80-120ms`.

## ValueCard

- Use after the SolutionDiagram.
- Desktop layout: 3 columns.
- Tablet layout: 2 columns.
- Mobile layout: 1 column.
- Padding desktop: `40-44px`.
- Padding mobile: `28px`.
- Minimum height desktop: `320px` (approximation).
- Radius: `tokens.radius.card`.
- Border: `tokens.border.strong`.
- Background: `linear-gradient(180deg, tokens.colors.bgPanel, tokens.colors.bgPanelDeep)`.
- Icon:
  - Size: `28-34px` (approximation).
  - Color: `tokens.colors.bluePrimary`.
- Title:
  - Typography: `tokens.typography.cardTitle`.
  - Color: `tokens.colors.bluePrimary`.
- Body:
  - Typography: `tokens.typography.cardBody`.
  - Color: `tokens.colors.textPrimary`.
- Do not nest cards inside ValueCard.

## FooterCTA

- Section min height: `tokens.layout.sectionMinHeightFooter`.
- Layout stack:
  - CTA headline.
  - CTA support copy.
  - Contact button.
  - Footer links and copyright.
  - Giant wordmark.
- CTA title typography: `tokens.typography.footerCtaTitle`.
- CTA support:
  - Font size: `18-20px` (approximation).
  - Line height: `1.4`.
  - Weight: `600`.
  - Color: `tokens.colors.textPrimary`.
- Button:
  - Same visual system as Header CTA.
  - Height: `44px`.
  - Padding: `0 22px`.
- Links:
  - Mono font.
  - Font size: `16px`.
  - Use arrow prefix.
  - Keep links in one quiet row on desktop.
  - Stack or wrap on mobile.
- Giant wordmark:
  - Text: `MERGEWARDEN` unless product naming requires `MERGE WARDEN`.
  - Typography: `tokens.typography.giantLogo`.
  - Color: `tokens.colors.white`.
  - Width: near full viewport with `52px` desktop side padding.
- Background:
  - Base: `tokens.colors.bgBase`.
  - Footer glow: `tokens.glow.footerRadial`.
  - Optional code texture behind CTA only.
- Footer must not look like a standard link-heavy footer.

## GlowBackground

- Purpose: shared background layer primitive.
- Must render four ordered layers:
  - Layer 1: solid base color `tokens.colors.bgBase`.
  - Layer 2: radial glow, section-specific.
  - Layer 3: code/data texture.
  - Layer 4: vignette.
- Radial glow:
  - Blur equivalent: `80-160px` (approximation).
  - Opacity: `0.55-0.85` for Hero; `0.2-0.55` for other sections.
- Texture:
  - Opacity: `0.12-0.25`.
  - Color: blue/white only.
  - Position absolute.
  - Use generated matrix text, CSS pseudo-elements, or SVG pattern.
  - Do not use colorful code screenshots.
- Vignette:
  - Opacity: `0.65-0.85`.
  - Dark corners.
- Hard rule: if text contrast drops, reduce texture opacity before changing text color.
