//! Icon paths from Lucide (ISC License, https://lucide.dev/license).
//!
//! Copied from lucide-static@0.454.0, the version the reference prototype pins.
//! Only the icons the console shell draws are here. Add one by copying its SVG
//! children into a match arm; the outer `<svg>` is the `Svg` wrapper below.

use dioxus::prelude::*;

/// The named Lucide icon. An unknown name draws nothing, so a typo is visible
/// as a missing glyph rather than a crash.
pub fn icon(name: &str) -> Element {
    match name {
        "activity" => rsx! {
            Svg {
            path { d: "M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2" }
            }
        },
        "bot" => rsx! {
            Svg {
            path { d: "M12 8V4H8" }
            rect { width: "16", height: "12", x: "4", y: "8", rx: "2" }
            path { d: "M2 14h2" }
            path { d: "M20 14h2" }
            path { d: "M15 13v2" }
            path { d: "M9 13v2" }
            }
        },
        "boxes" => rsx! {
            Svg {
            path { d: "M2.97 12.92A2 2 0 0 0 2 14.63v3.24a2 2 0 0 0 .97 1.71l3 1.8a2 2 0 0 0 2.06 0L12 19v-5.5l-5-3-4.03 2.42Z" }
            path { d: "m7 16.5-4.74-2.85" }
            path { d: "m7 16.5 5-3" }
            path { d: "M7 16.5v5.17" }
            path { d: "M12 13.5V19l3.97 2.38a2 2 0 0 0 2.06 0l3-1.8a2 2 0 0 0 .97-1.71v-3.24a2 2 0 0 0-.97-1.71L17 10.5l-5 3Z" }
            path { d: "m17 16.5-5-3" }
            path { d: "m17 16.5 4.74-2.85" }
            path { d: "M17 16.5v5.17" }
            path { d: "M7.97 4.42A2 2 0 0 0 7 6.13v4.37l5 3 5-3V6.13a2 2 0 0 0-.97-1.71l-3-1.8a2 2 0 0 0-2.06 0l-3 1.8Z" }
            path { d: "M12 8 7.26 5.15" }
            path { d: "m12 8 4.74-2.85" }
            path { d: "M12 13.5V8" }
            }
        },
        "building" => rsx! {
            Svg {
            rect { width: "16", height: "20", x: "4", y: "2", rx: "2", ry: "2" }
            path { d: "M9 22v-4h6v4" }
            path { d: "M8 6h.01" }
            path { d: "M16 6h.01" }
            path { d: "M12 6h.01" }
            path { d: "M12 10h.01" }
            path { d: "M12 14h.01" }
            path { d: "M16 10h.01" }
            path { d: "M16 14h.01" }
            path { d: "M8 10h.01" }
            path { d: "M8 14h.01" }
            }
        },
        "check" => rsx! {
            Svg {
            path { d: "M20 6 9 17l-5-5" }
            }
        },
        "chevron-down" => rsx! {
            Svg {
            path { d: "m6 9 6 6 6-6" }
            }
        },
        "chevrons-up-down" => rsx! {
            Svg {
            path { d: "m7 15 5 5 5-5" }
            path { d: "m7 9 5-5 5 5" }
            }
        },
        "circle-check" => rsx! {
            Svg {
            circle { cx: "12", cy: "12", r: "10" }
            path { d: "m9 12 2 2 4-4" }
            }
        },
        "circle-dashed" => rsx! {
            Svg {
            path { d: "M10.1 2.182a10 10 0 0 1 3.8 0" }
            path { d: "M13.9 21.818a10 10 0 0 1-3.8 0" }
            path { d: "M17.609 3.721a10 10 0 0 1 2.69 2.7" }
            path { d: "M2.182 13.9a10 10 0 0 1 0-3.8" }
            path { d: "M20.279 17.609a10 10 0 0 1-2.7 2.69" }
            path { d: "M21.818 10.1a10 10 0 0 1 0 3.8" }
            path { d: "M3.721 6.391a10 10 0 0 1 2.7-2.69" }
            path { d: "M6.391 20.279a10 10 0 0 1-2.69-2.7" }
            }
        },
        "door-open" => rsx! {
            Svg {
            path { d: "M13 4h3a2 2 0 0 1 2 2v14" }
            path { d: "M2 20h3" }
            path { d: "M13 20h9" }
            path { d: "M10 12v.01" }
            path { d: "M13 4.562v16.157a1 1 0 0 1-1.242.97L5 20V5.562a2 2 0 0 1 1.515-1.94l4-1A2 2 0 0 1 13 4.561Z" }
            }
        },
        "fingerprint" => rsx! {
            Svg {
            path { d: "M12 10a2 2 0 0 0-2 2c0 1.02-.1 2.51-.26 4" }
            path { d: "M14 13.12c0 2.38 0 6.38-1 8.88" }
            path { d: "M17.29 21.02c.12-.6.43-2.3.5-3.02" }
            path { d: "M2 12a10 10 0 0 1 18-6" }
            path { d: "M2 16h.01" }
            path { d: "M21.8 16c.2-2 .131-5.354 0-6" }
            path { d: "M5 19.5C5.5 18 6 15 6 12a6 6 0 0 1 .34-2" }
            path { d: "M8.65 22c.21-.66.45-1.32.57-2" }
            path { d: "M9 6.8a6 6 0 0 1 9 5.2v2" }
            }
        },
        "flask-conical" => rsx! {
            Svg {
            path { d: "M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2" }
            path { d: "M8.5 2h7" }
            path { d: "M7 16h10" }
            }
        },
        "git-pull-request-arrow" => rsx! {
            Svg {
            circle { cx: "5", cy: "6", r: "3" }
            path { d: "M5 9v12" }
            circle { cx: "19", cy: "18", r: "3" }
            path { d: "m15 9-3-3 3-3" }
            path { d: "M12 6h5a2 2 0 0 1 2 2v7" }
            }
        },
        "info" => rsx! {
            Svg {
            circle { cx: "12", cy: "12", r: "10" }
            path { d: "M12 16v-4" }
            path { d: "M12 8h.01" }
            }
        },
        "key-round" => rsx! {
            Svg {
            path { d: "M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z" }
            circle { cx: "16.5", cy: "7.5", r: ".5", fill: "currentColor" }
            }
        },
        "layout-dashboard" => rsx! {
            Svg {
            rect { width: "7", height: "9", x: "3", y: "3", rx: "1" }
            rect { width: "7", height: "5", x: "14", y: "3", rx: "1" }
            rect { width: "7", height: "9", x: "14", y: "12", rx: "1" }
            rect { width: "7", height: "5", x: "3", y: "16", rx: "1" }
            }
        },
        "list-todo" => rsx! {
            Svg {
            rect { x: "3", y: "5", width: "6", height: "6", rx: "1" }
            path { d: "m3 17 2 2 4-4" }
            path { d: "M13 6h8" }
            path { d: "M13 12h8" }
            path { d: "M13 18h8" }
            }
        },
        "loader-circle" => rsx! {
            Svg {
            path { d: "M21 12a9 9 0 1 1-6.219-8.56" }
            }
        },
        "lock" => rsx! {
            Svg {
            rect { width: "18", height: "11", x: "3", y: "11", rx: "2", ry: "2" }
            path { d: "M7 11V7a5 5 0 0 1 10 0v4" }
            }
        },
        "log-in" => rsx! {
            Svg {
            path { d: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4" }
            polyline { points: "10 17 15 12 10 7" }
            line { x1: "15", x2: "3", y1: "12", y2: "12" }
            }
        },
        "log-out" => rsx! {
            Svg {
            path { d: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" }
            polyline { points: "16 17 21 12 16 7" }
            line { x1: "21", x2: "9", y1: "12", y2: "12" }
            }
        },
        "message-square-lock" => rsx! {
            Svg {
            path { d: "M19 15v-2a2 2 0 1 0-4 0v2" }
            path { d: "M9 17H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v3.5" }
            rect { x: "13", y: "15", width: "8", height: "5", rx: "1" }
            }
        },
        "octagon-x" => rsx! {
            Svg {
            path { d: "m15 9-6 6" }
            path { d: "M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z" }
            path { d: "m9 9 6 6" }
            }
        },
        "scale" => rsx! {
            Svg {
            path { d: "m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z" }
            path { d: "m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z" }
            path { d: "M7 21h10" }
            path { d: "M12 3v18" }
            path { d: "M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2" }
            }
        },
        "server" => rsx! {
            Svg {
            rect { width: "20", height: "8", x: "2", y: "2", rx: "2", ry: "2" }
            rect { width: "20", height: "8", x: "2", y: "14", rx: "2", ry: "2" }
            line { x1: "6", x2: "6.01", y1: "6", y2: "6" }
            line { x1: "6", x2: "6.01", y1: "18", y2: "18" }
            }
        },
        "settings-2" => rsx! {
            Svg {
            path { d: "M20 7h-9" }
            path { d: "M14 17H5" }
            circle { cx: "17", cy: "17", r: "3" }
            circle { cx: "7", cy: "7", r: "3" }
            }
        },
        "triangle-alert" => rsx! {
            Svg {
            path { d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" }
            path { d: "M12 9v4" }
            path { d: "M12 17h.01" }
            }
        },
        "user-round" => rsx! {
            Svg {
            circle { cx: "12", cy: "8", r: "5" }
            path { d: "M20 21a8 8 0 0 0-16 0" }
            }
        },
        "users" => rsx! {
            Svg {
            path { d: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" }
            circle { cx: "9", cy: "7", r: "4" }
            path { d: "M22 21v-2a4 4 0 0 0-3-3.87" }
            path { d: "M16 3.13a4 4 0 0 1 0 7.75" }
            }
        },
        "waypoints" => rsx! {
            Svg {
            circle { cx: "12", cy: "4.5", r: "2.5" }
            path { d: "m10.2 6.3-3.9 3.9" }
            circle { cx: "4.5", cy: "12", r: "2.5" }
            path { d: "M7 12h10" }
            circle { cx: "19.5", cy: "12", r: "2.5" }
            path { d: "m13.8 17.7 3.9-3.9" }
            circle { cx: "12", cy: "19.5", r: "2.5" }
            }
        },
        _ => rsx! {},
    }
}

/// The shared `<svg>` frame every Lucide icon uses. Sizing comes from
/// `svg.lucide` in main.css, so the frame carries no width or height.
#[component]
fn Svg(children: Element) -> Element {
    rsx! {
        svg {
            class: "lucide",
            view_box: "0 0 24 24",
            fill: "none",
            stroke: "currentColor",
            stroke_width: "2",
            stroke_linecap: "round",
            stroke_linejoin: "round",
            "aria-hidden": "true",
            {children}
        }
    }
}
