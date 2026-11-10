# Source and publication scope

The owner has authorized publication of the generic UI portions under MIT.
Vue semantic primitives and v-model adapters derive from the common
`primitives.ts` (203 lines) in FastPPT and FastFigure. Modal's public props and
the focus-lifecycle purpose derive from their common `use-dialog-focus.ts`
(58 lines) and Modal component. FastUI delegates focus trapping, inertness,
nesting and focus restoration to the browser's native modal dialog rather
than maintaining the original hand-written trap. Select retains the shared
value/options/change contract and delegates interaction to the native select.

The palette follows FastWrite's semantic colors and the approved FastTask
workbench design. Only generic UI code and semantic design values are included.
No private artwork, icons, logos, fonts, application data, product-specific
workflows or brand assets are distributed. Upstream product repositories
retain ownership of their application-specific code and assets.

Source reference revisions: FastPPT `f6bf21f13445c2507719928fa68550552a8fcab2`
(`packages/components/src/`) and FastFigure
`13dcc4179fd0073da76f7626264af8605d5e8c70` (`src/components/ui/`).
At both revisions, `primitives.ts` has Git blob
`c7123af5f8316bc0486bce5c99cfbb2e9ab3dc73` and `use-dialog-focus.ts` has
Git blob `3ffbb6c6c7bd5e3c96f2b8c05c5e008dcdfc4cab`.
