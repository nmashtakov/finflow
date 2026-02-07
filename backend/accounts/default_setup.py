from django.contrib.auth.models import User
from django.db import transaction

from core.models import Category, ExpenseLink, Project, Subcategory


DEFAULT_PROJECT_NAME = "Личные финансы"
DEFAULT_STRUCTURE = {
    "Долг": ["Долг", "Кредит"],
    "Другие доходы": ["Кэшбек", "Подарки"],
    "Другие покупки": ["Другие покупки", "Одежда и обувь", "Техника"],
    "Еда": ["Готовая еда", "Супермаркет"],
    "Жилье": ["Оплата квартиры", "Кварплата"],
    "Здоровье": ["Медицинские расходы", "Стоматолог"],
    "ЗП": ["Работа"],
    "Переводы": ["Перевод между людьми", "Перевод между счетами"],
    "Подарки": ["Подарки"],
    "Развлечения": ["Развлечения"],
    "Транспорт": ["Машина", "Общественный транспорт", "Самолет/поезд", "Такси/каршеринг"],
    "Услуги и связь": ["Интернет и мобильная связь", "Подписки", "Услуги"],
    "Уход за собой": ["Парикмахерская", "Средства личной гигиены"],
}


@transaction.atomic
def create_default_finance_structure(user: User) -> None:
    project, _ = Project.objects.get_or_create(
        user=user,
        name=DEFAULT_PROJECT_NAME,
        defaults={"status": "active"},
    )

    if project.status != "active":
        project.status = "active"
        project.save(update_fields=["status"])

    for category_name, subcategory_names in DEFAULT_STRUCTURE.items():
        category, _ = Category.objects.get_or_create(
            user=user,
            name=category_name,
            defaults={"status": "active"},
        )
        if category.status != "active":
            category.status = "active"
            category.save(update_fields=["status"])

        ExpenseLink.objects.get_or_create(
            user=user,
            project=project,
            category=category,
            subcategory=None,
            defaults={"status": "active"},
        )

        for subcategory_name in subcategory_names:
            subcategory, _ = Subcategory.objects.get_or_create(
                user=user,
                name=subcategory_name,
                defaults={"status": "active"},
            )
            if subcategory.status != "active":
                subcategory.status = "active"
                subcategory.save(update_fields=["status"])

            ExpenseLink.objects.get_or_create(
                user=user,
                project=project,
                category=category,
                subcategory=subcategory,
                defaults={"status": "active"},
            )
