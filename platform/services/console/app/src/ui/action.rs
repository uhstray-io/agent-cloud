//! A disabled change control with its reason on the page. A disabled button takes no focus, so a
//! tooltip alone cannot reach a keyboard user. The reason is visible text beside the control, and
//! `aria-describedby` ties the two together (plan 17 D44, task 10.1).

use dioxus::prelude::*;

/// A disabled button labelled `label`, with `reason` shown as visible text beside it. The button's
/// `aria-describedby` names `id`, the element that holds the reason, so a screen reader reads the reason
/// with the control. `id` must be unique on the page. The tooltip stays as it was.
#[component]
pub fn DisabledAction(label: String, reason: &'static str, id: String) -> Element {
    rsx! {
        button {
            class: "btn",
            disabled: true,
            aria_describedby: "{id}",
            title: reason,
            "{label}"
        }
        span { id: "{id}", class: "action-reason", "{reason}" }
    }
}
