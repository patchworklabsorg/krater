# frozen_string_literal: true
#
# Provisions a local Weave with everything the live Krater<->Weave e2e check
# needs: three users (a member, an admin and a non-member), a confidential
# OAuth application for Krater with the scopes Krater asks for, plus
# `directory` for its client_credentials token, and Krater's app roles
# (member, reviewer, admin) on that application. Idempotent (upsert-by-email),
# so it can be re-run against the same dev database.
#
# Weave owns Krater's roles (patchworklabsorg/weave#165, #166): the member
# gets `member`, the admin gets `member` and `admin`, and the non-member gets
# nothing. The application stays open to everyone, so Weave lets the
# non-member through and Krater itself has to refuse them.
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
app = Doorkeeper::Application.create!(
  name: app_name,
  redirect_uri: redirect_uri,
  confidential: true,
  # Open to everyone, so the non-member reaches Krater's own refusal page
  # rather than being stopped at Weave's authorize step.
  access_policy: "everyone",
  scopes: "openid profile email groups roles slack directory"
)
log "oauth application ready: uid=#{app.uid}"

# -- Krater's app roles ------------------------------------------------------------------------

# Roles belong to the application, so destroying it above dropped them too (a
# database cascade, with no callbacks or jobs). Create them fresh each run.
roles = {
  "member" => "Ganymede member",
  "reviewer" => "Krater reviewer",
  "admin" => "Krater admin"
}.to_h do |key, name|
  [key, ApplicationRole.create!(application: app, key: key, name: name)]
end

{ member => %w[member], admin => %w[member admin] }.each do |user, keys|
  keys.each { |key| roles.fetch(key).assignments.create!(assignee: user) }
end
log "app roles ready: #{roles.keys.join(', ')}"

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
