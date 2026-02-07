from datetime import datetime, time
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import TruncDay
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.utils import timezone

from .forms import ProjectForm, CategoryForm, SubcategoryForm, AccountForm
from core.models import Project, Category, Subcategory, ExpenseLink, Account, UserPreferences, Transaction


def landing_view(request):
    return render(request, 'landing.html')

@login_required
def dashboard_view(request):
    user = request.user
    now = timezone.localtime()
    default_start = datetime(now.year, now.month, 1)
    period_start = timezone.make_aware(default_start, timezone.get_current_timezone())

    start_param = request.GET.get('start')
    end_param = request.GET.get('end')
    account_param = request.GET.get('account')
    project_param = request.GET.get('project')

    if start_param:
        try:
            parsed = datetime.strptime(start_param, '%Y-%m-%d')
            period_start = timezone.make_aware(
                datetime.combine(parsed.date(), time.min),
                timezone.get_current_timezone()
            )
        except ValueError:
            pass

    period_end = timezone.make_aware(datetime.combine(now.date(), time.max), timezone.get_current_timezone())
    if end_param:
        try:
            parsed = datetime.strptime(end_param, '%Y-%m-%d')
            period_end = timezone.make_aware(
                datetime.combine(parsed.date(), time.max),
                timezone.get_current_timezone()
            )
        except ValueError:
            pass

    transactions_qs = Transaction.objects.filter(account__user=user)
    filtered_qs = transactions_qs.filter(date__range=(period_start, period_end))

    if account_param and account_param.isdigit():
        filtered_qs = filtered_qs.filter(account_id=int(account_param))
    if project_param and project_param.isdigit():
        filtered_qs = filtered_qs.filter(expense_link__project_id=int(project_param))

    total_income = filtered_qs.filter(amount__gt=0).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    total_expense = filtered_qs.filter(amount__lt=0).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    account_balances_raw = (
        filtered_qs
        .values('account__name', 'account__currency')
        .annotate(balance=Sum('amount'))
        .order_by('account__name')
    )

    top_expense_categories_raw = (
        filtered_qs
        .filter(amount__lt=0)
        .values('expense_link__category__name')
        .annotate(total=Sum('amount'))
        .order_by('total')[:5]
    )

    trend_data = (
        filtered_qs
        .annotate(day=TruncDay('date'))
        .values('day')
        .annotate(total=Sum('amount'))
        .order_by('day')
    )

    recent_transactions_qs = (
        filtered_qs
        .select_related('account', 'expense_link__project', 'expense_link__category')
        .order_by('-date')[:5]
    )
    recent_transactions = []
    for tx in recent_transactions_qs:
        amount_value = float(tx.amount)
        amount_text = f"{abs(amount_value):,.2f}".replace(',', ' ').replace('.', ',')
        if amount_text.endswith(',00'):
            amount_text = amount_text[:-3]
        amount_text = f"{'+' if amount_value >= 0 else '-'}{amount_text} {tx.currency}"
        recent_transactions.append({
            'date': timezone.localtime(tx.date).strftime('%d.%m.%Y %H:%M'),
            'account': tx.account.name,
            'project': tx.expense_link.project.name,
            'category': tx.expense_link.category.name,
            'amount': amount_text,
            'is_income': amount_value >= 0,
            'comment': tx.comment or '—',
        })

    def format_amount(value):
        text = f"{Decimal(value or 0):,.2f}".replace(',', ' ').replace('.', ',')
        if text.endswith(',00'):
            text = text[:-3]
        return text

    net_amount = total_income + total_expense
    total_expense_abs = abs(total_expense)
    has_expense = total_expense_abs > 0
    if not has_expense:
        total_expense_abs = Decimal('1')

    trend_labels = [data['day'].strftime('%d.%m') for data in trend_data]
    trend_values = [float(data['total']) for data in trend_data]

    filters = {
        'start': period_start.strftime('%Y-%m-%d'),
        'end': period_end.strftime('%Y-%m-%d'),
        'account': account_param or '',
        'project': project_param or '',
    }

    accounts = Account.objects.filter(user=user, status='active').order_by('name')
    projects = Project.objects.filter(user=user, status='active').order_by('name')

    context = {
        'total_income': format_amount(total_income),
        'total_expense': format_amount(abs(total_expense)),
        'net_amount': format_amount(net_amount),
        'net_positive': net_amount >= 0,
        'operation_count': filtered_qs.count(),
        'filters': filters,
        'accounts': accounts,
        'projects': projects,
        'account_balances': [
            {
                'name': row['account__name'],
                'currency': row['account__currency'],
                'balance': format_amount(row['balance'] or 0),
                'is_negative': (row['balance'] or 0) < 0,
            }
            for row in account_balances_raw
        ],
        'top_expense_categories': [
            {
                'name': row['expense_link__category__name'] or '—',
                'total': format_amount(abs(row['total'] or 0)),
                'percent': 0 if not has_expense else round((abs(row['total'] or 0) / total_expense_abs) * 100, 1)
            }
            for row in top_expense_categories_raw
        ],
        'trend_labels': trend_labels,
        'trend_values': trend_values,
        'recent_transactions': recent_transactions,
    }
    return render(request, 'dashboard.html', context)

@login_required
def categories_settings(request):
    user = request.user
    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    if preferences.default_project and preferences.default_project.status != 'active':
        preferences.default_project = None
        preferences.save(update_fields=['default_project'])

    open_project_id = None
    open_category_id = None
    open_param = request.GET.get('open')
    category_param = request.GET.get('category')
    if open_param and open_param.isdigit():
        open_project_id = int(open_param)

    if category_param and category_param.isdigit():
        open_category_id = int(category_param)

    if request.method == "POST" and 'set_default_project' in request.POST:
        project_id = request.POST.get('project_id')
        project = get_object_or_404(Project, pk=project_id, user=user, status='active')
        preferences.default_project = project
        preferences.save()
        target = project.id
        category_param = request.POST.get('category_id')
        url = f"{reverse('categories_settings')}?open={target}"
        if category_param:
            url += f"&category={category_param}"
        return redirect(url)

    # Обработка редактирования проекта
    if request.method == "POST" and 'edit_project' in request.POST:
        project_id = request.POST.get('project_id')
        project = get_object_or_404(Project, pk=project_id, user=user, status='active')
        project.name = (request.POST.get('name') or project.name).strip()
        project.description = (request.POST.get('description') or '').strip()
        if project.name:
            project.save(update_fields=['name', 'description'])
        return redirect(f"{reverse('categories_settings')}?open={project.id}")

    # Обработка удаления проекта
    if request.method == "POST" and 'delete_project' in request.POST:
        project_id = request.POST.get('project_id')
        project = get_object_or_404(Project, pk=project_id, user=user, status='active')
        project.status = 'deleted'
        project.save(update_fields=['status'])
        ExpenseLink.objects.filter(user=user, project=project, status='active').update(status='deleted')
        if preferences.default_project_id == project.id:
            preferences.default_project = None
            preferences.save(update_fields=['default_project'])
        return redirect('categories_settings')

    # Обработка добавления проекта
    if request.method == "POST" and 'add_project' in request.POST:
        project_form = ProjectForm(request.POST)
        if project_form.is_valid():
            new_project = project_form.save(commit=False)
            new_project.user = user
            new_project.status = 'active'
            new_project.save()
            return redirect(f"{reverse('categories_settings')}?open={new_project.id}")
    else:
        project_form = ProjectForm()

    # Обработка добавления категории
    if request.method == "POST" and 'add_category' in request.POST:
        category_form = CategoryForm(request.POST)
        project_id = request.POST.get('project_id')
        if category_form.is_valid() and project_id:
            new_category = category_form.save(commit=False)
            new_category.user = user
            new_category.status = 'active'
            new_category.save()
            # Cоздаем ExpenseLink
            ExpenseLink.objects.create(
                user=user,
                project_id=project_id,
                category=new_category,
                status='active'
            )
            return redirect(f"{reverse('categories_settings')}?open={project_id}&category={new_category.id}")
    else:
        category_form = CategoryForm()

    # Обработка редактирования категории
    if request.method == "POST" and 'edit_category' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        get_object_or_404(
            ExpenseLink,
            user=user,
            project_id=project_id,
            category_id=category_id,
            status='active'
        )
        category = get_object_or_404(Category, pk=category_id, user=user, status='active')
        category.name = (request.POST.get('name') or category.name).strip()
        if category.name:
            category.save(update_fields=['name'])
        return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category.id}")

    # Обработка удаления категории
    if request.method == "POST" and 'delete_category' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        get_object_or_404(
            ExpenseLink,
            user=user,
            project_id=project_id,
            category_id=category_id,
            status='active'
        )
        category = get_object_or_404(Category, pk=category_id, user=user, status='active')

        links_to_delete = ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category=category,
            status='active'
        )
        subcategory_ids = list(
            links_to_delete.exclude(subcategory__isnull=True).values_list('subcategory_id', flat=True).distinct()
        )
        links_to_delete.update(status='deleted')
        has_active_category_links = ExpenseLink.objects.filter(
            user=user, category=category, status='active'
        ).exists()
        if not has_active_category_links:
            category.status = 'deleted'
            category.save(update_fields=['status'])

        for subcategory_id in subcategory_ids:
            has_active_links = ExpenseLink.objects.filter(
                user=user, subcategory_id=subcategory_id, status='active'
            ).exists()
            if not has_active_links:
                Subcategory.objects.filter(pk=subcategory_id, user=user, status='active').update(status='deleted')

        return redirect(f"{reverse('categories_settings')}?open={project_id}")

    # Обработка добавления подкатегории
    if request.method == "POST" and 'add_subcategory' in request.POST:
        subcategory_form = SubcategoryForm(request.POST)
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        if subcategory_form.is_valid() and project_id and category_id:
            new_subcategory = subcategory_form.save(commit=False)
            new_subcategory.user = user
            new_subcategory.status = 'active'
            new_subcategory.save()
            ExpenseLink.objects.create(
                user=user,
                project_id=project_id,
                category_id=category_id,
                subcategory=new_subcategory,
                status='active'
            )
            return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category_id}")
    else:
        subcategory_form = SubcategoryForm()

    # Обработка редактирования подкатегории
    if request.method == "POST" and 'edit_subcategory' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        subcategory_id = request.POST.get('subcategory_id')
        get_object_or_404(
            ExpenseLink,
            user=user,
            project_id=project_id,
            category_id=category_id,
            subcategory_id=subcategory_id,
            status='active'
        )
        subcategory = get_object_or_404(Subcategory, pk=subcategory_id, user=user, status='active')
        subcategory.name = (request.POST.get('name') or subcategory.name).strip()
        if subcategory.name:
            subcategory.save(update_fields=['name'])
        return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category_id}")

    # Обработка удаления подкатегории
    if request.method == "POST" and 'delete_subcategory' in request.POST:
        project_id = request.POST.get('project_id')
        category_id = request.POST.get('category_id')
        subcategory_id = request.POST.get('subcategory_id')
        get_object_or_404(
            ExpenseLink,
            user=user,
            project_id=project_id,
            category_id=category_id,
            subcategory_id=subcategory_id,
            status='active'
        )
        subcategory = get_object_or_404(Subcategory, pk=subcategory_id, user=user, status='active')
        ExpenseLink.objects.filter(
            user=user,
            project_id=project_id,
            category_id=category_id,
            subcategory=subcategory,
            status='active'
        ).update(status='deleted')

        has_active_links = ExpenseLink.objects.filter(
            user=user, subcategory=subcategory, status='active'
        ).exists()
        if not has_active_links:
            subcategory.status = 'deleted'
            subcategory.save(update_fields=['status'])
        return redirect(f"{reverse('categories_settings')}?open={project_id}&category={category_id}")

    # Cтроим дерево, как раньше:
    projects = Project.objects.filter(user=user, status="active")
    expense_links = ExpenseLink.objects.filter(user=user, status="active")
    tree = []
    project_categories_map = {}
    for project in projects:
        links_in_project = expense_links.filter(project=project)
        categories = Category.objects.filter(
            id__in=links_in_project.values_list('category', flat=True).distinct(),
            status='active'
        )
        cat_list = []
        for category in categories:
            links_in_category = links_in_project.filter(category=category)
            subcat_ids = links_in_category.values_list('subcategory', flat=True).distinct()
            subcategories = Subcategory.objects.filter(
                id__in=[sid for sid in subcat_ids if sid],
                status='active'
            )
            cat_list.append({'category': category, 'subcategories': subcategories})
        project_categories_map[project.id] = [item['category'].id for item in cat_list]
        tree.append({'project': project, 'categories': cat_list})

    if open_project_id is None:
        open_project_id = None

    if open_project_id and not open_category_id:
        category_ids = project_categories_map.get(open_project_id, [])
        if category_ids:
            open_category_id = category_ids[0]

    # validate provided category belongs to project
    if open_project_id and open_category_id:
        valid_ids = set(project_categories_map.get(open_project_id, []))
        if open_category_id not in valid_ids and valid_ids:
            open_category_id = next(iter(valid_ids))

    context = {
        'tree': tree,
        'project_form': project_form,
        'category_form': category_form,
        'subcategory_form': subcategory_form,
        'default_project_id': preferences.default_project_id,
        'open_project_id': open_project_id,
        'open_category_id': open_category_id,
    }
    return render(request, 'categories.html', context)


@login_required
def accounts_directory(request):
    user = request.user
    preferences, _ = UserPreferences.objects.get_or_create(user=user)
    if preferences.default_account and preferences.default_account.status != 'active':
        preferences.default_account = None
        preferences.save(update_fields=['default_account'])
    accounts = Account.objects.filter(user=user, status='active').order_by('name')

    form = AccountForm()
    edit_forms = {}
    modal_to_open = None

    if request.method == "POST":
        if 'create_account' in request.POST:
            form = AccountForm(request.POST)
            if form.is_valid():
                account = form.save(commit=False)
                account.user = user
                account.status = 'active'
                if Account.objects.filter(user=user, name__iexact=account.name).exists():
                    form.add_error('name', 'Счёт с таким названием уже существует')
                else:
                    account.save()
                    return redirect('accounts_directory')
            modal_to_open = 'accountModal'
        elif 'edit_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id, user=user, status='active')
            edit_form = AccountForm(request.POST, instance=account)
            if edit_form.is_valid():
                new_name = edit_form.cleaned_data['name']
                if Account.objects.filter(user=user, name__iexact=new_name).exclude(pk=account.id).exists():
                    edit_form.add_error('name', 'Счёт с таким названием уже существует')
                else:
                    edit_form.save()
                    return redirect('accounts_directory')
            edit_forms[account.id] = edit_form
            modal_to_open = f'editAccountModal{account.id}'
        elif 'delete_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id, user=user, status='active')
            account.status = 'deleted'
            account.save()
            if preferences.default_account_id == account.id:
                preferences.default_account = None
                preferences.save(update_fields=['default_account'])
            return redirect('accounts_directory')
        elif 'set_default_account' in request.POST:
            account_id = request.POST.get('account_id')
            account = get_object_or_404(Account, pk=account_id, user=user, status='active')
            preferences.default_account = account
            preferences.save()
            return redirect('accounts_directory')

    accounts_with_forms = []
    for account in accounts:
        edit_form = edit_forms.get(account.id, AccountForm(instance=account))
        accounts_with_forms.append((account, edit_form))

    context = {
        'accounts_with_forms': accounts_with_forms,
        'form': form,
        'modal_to_open': modal_to_open,
        'default_account_id': preferences.default_account_id,
    }
    return render(request, 'accounts/account-directory.html', context)
