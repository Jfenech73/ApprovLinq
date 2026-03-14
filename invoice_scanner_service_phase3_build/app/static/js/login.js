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
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Login failed");
    localStorage.setItem("approvlinq_token", data.access_token);
    const defaultTenant = (data.tenants || []).find(t => t.is_default) || (data.tenants || [])[0];
    if (defaultTenant) localStorage.setItem("approvlinq_tenant_id", defaultTenant.tenant_id);
    window.location.href = data.landing_page;
  } catch (error) {
    message.textContent = error.message;
  }
});
