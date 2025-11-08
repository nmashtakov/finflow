from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from .forms import ProjectForm, CategoryForm, SubcategoryForm, AccountForm
from core.models import Project, Category, Subcategory, ExpenseLink, Account, UserPreferences


def landing_view(request):
    return render(request, 'landing.html')

def dashboard_view(request):
    return render(request, 'dashboard.html')

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

    # Cтроим дерево, как раньше:
    projects = Project.objects.filter(user=user, status="active")
    expense_links = ExpenseLink.objects.filter(user=user, status="active")
    tree = []
    project_categories_map = {}
    for project in projects:
        links_in_project = expense_links.filter(project=project)
        categories = Category.objects.filter(id__in=links_in_project.values_list('category', flat=True).distinct())
        cat_list = []
        for category in categories:
            links_in_category = links_in_project.filter(category=category)
            subcat_ids = links_in_category.values_list('subcategory', flat=True).distinct()
            subcategories = Subcategory.objects.filter(id__in=[sid for sid in subcat_ids if sid])
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
