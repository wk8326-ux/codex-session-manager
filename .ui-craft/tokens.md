# Codex Session Manager tokens

## Visual direction

- **Style:** restrained conversation operations workbench
- **Signature:** live session state is always visible beside the latest conversation activity
- **Density:** medium-high on desktop, progressive disclosure on mobile

## Color

- Canvas: `#f4f4f4`
- Raised surface: `#ffffff`
- Sidebar: `#ebebeb`
- Primary ink: `#202020`
- Secondary ink: `#676767`
- Accent forest: `#2d715b`
- Running: `#27845a`
- Warning: `#ad6b1d`
- Danger: `#b44538`

Color is reserved for actions, focus, and semantic state. The base interface stays neutral.

Dark mode uses `#181818` canvas, `#212121` raised surfaces, and `#141414` navigation. Green never tints the full canvas or ordinary selected rows.

## Typography

- Display: `Bahnschrift`, `Microsoft YaHei UI`, system sans-serif
- Body: `Microsoft YaHei UI`, `Segoe UI`, system sans-serif
- Utility/data: `Cascadia Mono`, `Consolas`, monospace
- Letter spacing remains `0` throughout.
- Desktop body copy is `11-13px`; navigation and metadata use `9-12px`; page titles use `18-20px`.
- Desktop controls use a compact `34px` visual height; touch layouts restore a `44px` hit target.
- Session rows stay compact enough to compare state without shrinking mobile touch targets.

## Shape and depth

- Inputs: `6px`
- Buttons: `6px`
- Service surface: `8px`
- Dialogs: `8px`
- Raised surfaces use a two-layer cool-tinted shadow.

## Motion

- Hover and pressed feedback: `120-160ms`
- Dialog entrance: native dialog behavior with a short opacity transition only
- All motion is removed when `prefers-reduced-motion: reduce` is active
