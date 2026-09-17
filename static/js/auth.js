// static/js/auth.js
// Shared behavior for the auth pages (login, register, forgot/reset
// password, 2FA verify) -- inert everywhere else, since none of these
// selectors match outside that family. Password show/hide toggles
// scope their eye icons to their own button (querySelector, not a
// global id) so a page can carry more than one, like reset password's
// new/confirm pair.

document.querySelectorAll(".auth-toggle-btn").forEach(function (toggleBtn) {
    const input = document.getElementById(toggleBtn.getAttribute("aria-controls"));
    const eyeIcon = toggleBtn.querySelector(".auth-toggle-icon-eye");
    const eyeOffIcon = toggleBtn.querySelector(".auth-toggle-icon-eye-off");
    if (!input) return;

    toggleBtn.addEventListener("click", function () {
        const visible = input.type === "password";
        input.type = visible ? "text" : "password";
        toggleBtn.setAttribute("aria-pressed", String(visible));
        toggleBtn.setAttribute("aria-label", visible ? "Hide password" : "Show password");
        eyeIcon?.classList.toggle("hidden", visible);
        eyeOffIcon?.classList.toggle("hidden", !visible);
    });
});

// The only password rule the backend enforces is an 8-character
// minimum (auth/routes.py) -- this just reflects that back live rather
// than leaving it to a rejected submit and a flash message.
document.querySelectorAll("[data-min-length-hint]").forEach(function (input) {
    const hint = document.getElementById(input.getAttribute("data-min-length-hint"));
    const minLength = parseInt(input.getAttribute("data-min-length"), 10) || 0;
    if (!hint) return;
    input.addEventListener("input", function () {
        hint.classList.toggle("auth-hint-met", input.value.length >= minLength);
    });
});

document.querySelectorAll(".auth-card form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
        // Reset password is the only auth form with two password
        // fields to compare -- checked here, inline, rather than
        // waiting on a full round trip just to be told they didn't
        // match.
        const a = form.querySelector("#new_password");
        const b = form.querySelector("#confirm_password");
        if (a && b && a.value !== b.value) {
            event.preventDefault();
            import("./toast.js").then(function (mod) {
                mod.showToast("Those passwords don't match.", "error");
            });
            return;
        }

        // A full-page POST leaves the button just sitting there for
        // however long the round trip takes -- this is the only signal
        // the click actually registered. The label stays in the DOM
        // (only visually hidden) so a slow connection never shows an
        // unlabeled button.
        const submitBtn = form.querySelector("button[type=submit].auth-button");
        submitBtn?.classList.add("is-loading");
    });
});
