document.getElementById("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;
  const message = document.getElementById("loginMessage");
  message.textContent = "Signing in...";

  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    const contentType = response.headers.get("content-type") || "";
    let data = null;

    if (contentType.includes("application/json")) {
      data = await response.json();
    } else {
      const text = await response.text();
      throw new Error(
        text && text.includes("Internal Server Error")
          ? "Something went wrong on the server. Please try again."
          : (text || "Login failed.")
      );
    }

    if (!response.ok) {
      throw new Error(data?.detail || "Login failed.");
    }

    localStorage.setItem("approvlinq_token", data.access_token);
    const defaultTenant = (data.tenants || []).find(t => t.is_default) || (data.tenants || [])[0];
    if (defaultTenant) localStorage.setItem("approvlinq_tenant_id", defaultTenant.tenant_id);
    window.location.href = data.landing_page;
  } catch (error) {
    message.textContent = error.message;
  }
});

initPageHelp({
  title: "Login page help",
  subtitle: "Use this page to access the right workspace for your role.",
  sections: [
    { heading: "How to sign in", items: ["Enter the email address created for you by Approvlinq or your platform administrator.", "Enter your current password and click Sign in.", "The system will redirect you to Platform Admin or Tenant Admin depending on your role."] },
    { heading: "If sign-in fails", items: ["Check spelling in the email address.", "Make sure caps lock is off.", "Confirm your tenant or user has not been marked inactive.", "If needed, ask your administrator to reset your password."] },
    { heading: "Security tips", items: ["Do not share credentials.", "Change the temporary password after first access.", "Use the Logout button when you finish on shared devices."] },
    { heading: "What happens next", items: ["Platform admins manage tenants, users, capacity and issue triage.", "Tenant users manage company data, suppliers, nominal accounts and scanning work."] }
  ],
  quickChecks: ["Use the email assigned to you.", "If you were just created, wait a few seconds and try again.", "Contact support if the account remains inactive."]
});
