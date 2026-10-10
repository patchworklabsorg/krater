# frozen_string_literal: true
#
# Provisions a local Weave with everything the live Krater<->Weave e2e check
# needs: three users (a member, an admin and a non-member), a confidential
# OAuth application for Krater with the scopes Krater asks for plus
# `directory` and `quilt` for its client_credentials tokens, and Krater's app
# roles on that application. The `quilt` scope needs patchworklabsorg/weave#176. Idempotent (upsert-by-email), so it can be re-run against the
# same dev database.
#
# Weave owns Krater's roles (patchworklabsorg/weave#165, #166). This script
# creates the roles `member`, `reviewer` and `admin` on the app (the
# ApplicationRole model), and gives the member `member` and the admin `member`
# and `admin` (ApplicationRoleAssignment). The non-member gets no role. The
# result says `roles_provisioned: true`, so the live role tests run.
#
# This file lives in the Krater repo (it's Krater's e2e fixture, not a Weave
# behavior change) but runs inside a Weave checkout:
#
#   cd /path/to/weave
#   bundle exec rails runner /path/to/krater/scripts/dev/weave_e2e_provision.rb
#
# See scripts/dev/weave_e2e_setup.py for a wrapper that also writes the
# KRATER_* env vars this produces into a fixture file, and
# docs/dev/weave-e2e.md for the full workflow.
#
# Prints one JSON object to stdout with everything the Python side needs
# (client_id/secret, user emails/subs). Nothing else should be printed to
# stdout -- logs go to stderr.

require "json"

def log(msg) = warn("[weave_e2e_provision] #{msg}")

def upsert_user(email:, first_name:, last_name:)
  user = User.find_or_initialize_by(email: email)
  user.first_name = first_name
  user.last_name = last_name
  if user.new_record?
    user.password = User.generate_secure_password
    user.email_confirmed_at = Time.current
  end
  # Weave refuses every app to a user who hasn't accepted the Code of Conduct
  # (AppAccess, patchworklabsorg/weave#171), so the fixture users accept it.
  user.slack_coc_accepted_at ||= Time.current
  user.save!
  user
end

member = upsert_user(email: "e2e-member@ganymede.test", first_name: "Mira", last_name: "Member")
admin = upsert_user(email: "e2e-admin@ganymede.test", first_name: "Avi", last_name: "Admin")
non_member = upsert_user(email: "e2e-nonmember@ganymede.test", first_name: "Nia", last_name: "Nonmember")
log "users ready: #{[member, admin, non_member].map(&:email).join(', ')}"

# -- OAuth application for Krater --------------------------------------------------------------

redirect_uri = ENV.fetch("KRATER_REDIRECT_URI", "http://localhost:8201/auth/callback")
app_name = "Krater (e2e)"
# Secrets are hashed at rest (hash_application_secrets), so the plaintext is
# only ever available on the record returned by .create!. Recreate each run
# rather than updating, so this script always knows the current secret.
Doorkeeper::Application.where(name: app_name).destroy_all
# `access_policy: "everyone"` lets the non-member through Weave, so the check
# proves that Krater itself refuses someone without the `member` role.
app = Doorkeeper::Application.create!(
  name: app_name,
  redirect_uri: redirect_uri,
  confidential: true,
  scopes: "openid profile email groups roles slack directory quilt",
  access_policy: "everyone",
  requires_code_of_conduct: true
)
log "oauth application ready: uid=#{app.uid}"

# -- Krater's app roles ------------------------------------------------------------------------

role_names = {
  "member" => "Member",
  "reviewer" => "Reviewer",
  "admin" => "Admin"
}

roles = role_names.to_h do |key, name|
  role = ApplicationRole.find_or_create_by!(application: app, key: key) { |r| r.name = name }
  [key, role]
end

def assign_role(role, user)
  ApplicationRoleAssignment.find_or_create_by!(role: role, assignee: user)
end

assign_role(roles.fetch("member"), member)
assign_role(roles.fetch("member"), admin)
assign_role(roles.fetch("admin"), admin)
log "app roles ready: #{roles.keys.join(', ')}; member=[member], admin=[member, admin], non_member=[]"

result = {
  issuer: (ENV["OIDC_ISSUER"].presence || "http://localhost:3000"),
  oauth_client_id: app.uid,
  oauth_client_secret: app.plaintext_secret || app.secret,
  redirect_uri: redirect_uri,
  roles_provisioned: true,
  users: {
    member: { email: member.email, sub: member.p_id },
    admin: { email: admin.email, sub: admin.p_id },
    non_member: { email: non_member.email, sub: non_member.p_id }
  }
}

puts JSON.generate(result)
