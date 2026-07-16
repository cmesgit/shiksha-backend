# PLACEMENT: backend/accounts/management/commands/bootstrap_admin.py
#
# Grants the ADMIN role to an existing user by email. Nothing else in the
# codebase creates the first UserRole(ADMIN) row: is_staff/is_superuser
# (from `createsuperuser`) are never translated into an ADMIN role, and the
# Admin-dashboard's login gate checks the roles list, not is_staff — so a
# fresh superuser is bounced at the React login screen until this is run.
#
#     python manage.py bootstrap_admin someone@example.com

from django.core.management.base import BaseCommand, CommandError

from accounts.models import Role, User, UserRole


class Command(BaseCommand):
    help = "Grant the ADMIN role to an existing user, so they can log into the Admin-dashboard."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email of the user to grant ADMIN.")

    def handle(self, *args, **options):
        email = options["email"]
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(f'No user found with email "{email}".')

        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])

        role, _ = Role.objects.get_or_create(name=Role.ADMIN)
        _, created = UserRole.objects.update_or_create(
            user=user, role=role, defaults={"is_active": True}
        )

        self.stdout.write(self.style.SUCCESS(
            f'{"Granted" if created else "Refreshed"} ADMIN role for {user.email}. '
            "They can now log into the Admin-dashboard."
        ))
