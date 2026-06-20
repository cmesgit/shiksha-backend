# Account signals.
#
# The legacy one-User-one-Profile auto-create signal has been removed along
# with the Profile model. Learner identities now live on LearnerProfile and
# are created explicitly by the signup flow (signup_serializer) and lazily
# ensured at login via auth_flow._ensure_default_profile(). There is nothing
# to auto-create on User save anymore; this module is kept so the import in
# AccountsConfig.ready() stays valid and future signals have a home.
