from datetime import datetime

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, get_object_or_404, render
from django_q.tasks import async_task, schedule

from auth.helpers import auth_required
from auth.models import Code, Session
from bot.common import send_telegram_message, ADMIN_CHAT
from club.exceptions import BadRequest, AccessDenied
from gdpr.forget import delete_user_data
from gdpr.models import DataRequests
from notifications.email.users import send_delete_account_request_email, send_delete_account_confirm_email
from users.models.user import User


@auth_required
def request_delete_account(request, user_slug):
    if request.method != "POST":
        return redirect("edit_account", user_slug, permanent=False)

    user = get_object_or_404(User, slug=user_slug)
    if user.id != request.me.id and not request.me.is_god:
        raise Http404()

    confirmation_string = request.POST.get("confirm")
    if confirmation_string != settings.GDPR_DELETE_CONFIRMATION:
        raise BadRequest(
            title="Неправильная строка подтверждения",
            message=f"Вы должны в точности написать \"{settings.GDPR_DELETE_CONFIRMATION}\" "
                    f"чтобы запустить процедуру удаления аккаунта"
        )

    DataRequests.register_forget_request(user)

    code = Code.create_for_user(user=user, recipient=user.email, length=settings.GDPR_DELETE_CODE_LENGTH)
    async_task(
        send_delete_account_request_email,
        user=user,
        code=code
    )

    return render(request, "users/messages/delete_account_requested.html", {"user": user})


@auth_required
def confirm_delete_account(request, user_slug):
    if request.method != "POST":
        return redirect("edit_account", user_slug, permanent=False)

    user = get_object_or_404(User, slug=user_slug)
    if user.id != request.me.id and not request.me.is_god:
        raise Http404()

    confirmation_hash = request.POST.get("secret_hash")
    code = request.POST.get("code")
    if confirmation_hash != user.secret_hash or not code:
        raise AccessDenied(
            title="Что-то не сходится",
            message="Проверьте правильность кода и попробуйте запросить удаление аккаунта еще раз"
        )

    # verify code (raises an exception)
    Code.check_code(recipient=user.email, code=code)

    # mark user for future deletion
    user.deleted_at = datetime.utcnow()
    user.save()

    # remove sessions
    Session.objects.filter(user=user).delete()

    # schedule data cleanup
    schedule(delete_user_data, user, next_run=datetime.utcnow() + settings.GDPR_DELETE_TIMEDELTA)

    # notify user
    async_task(
        send_delete_account_confirm_email,
        user=user,
    )

    # notify admins
    async_task(
        send_telegram_message,
        chat=ADMIN_CHAT,
        text=f"💀 Юзер удалился: {settings.APP_HOST}/user/{user.slug}/",
    )

    return render(request, "users/messages/delete_account_confirmed.html",)
