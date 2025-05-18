from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from .forms import ProjectForm, CategoryForm, SubcategoryForm
from core.models import Project, Category, Subcategory, ExpenseLink


def landing_view(request):
    return render(request, 'landing.html')

def dashboard_view(request):
    return render(request, 'dashboard.html')

@login_required
def categories_settings(request):
    user = request.user

    # Обработка добавления проекта
    if request.method == "POST" and 'add_project' in request.POST:
        project_form = ProjectForm(request.POST)
        if project_form.is_valid():
            new_project = project_form.save(commit=False)
            new_project.user = user
            new_project.status = 'active'
            new_project.save()
            return redirect('categories_settings')
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
            return redirect('categories_settings')
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
            return redirect('categories_settings')
    else:
        subcategory_form = SubcategoryForm()

    # Cтроим дерево, как раньше:
    projects = Project.objects.filter(user=user, status="active")
    expense_links = ExpenseLink.objects.filter(user=user, status="active")
    tree = []
    for project in projects:
        links_in_project = expense_links.filter(project=project)
        categories = Category.objects.filter(id__in=links_in_project.values_list('category', flat=True).distinct())
        cat_list = []
        for category in categories:
            links_in_category = links_in_project.filter(category=category)
            subcat_ids = links_in_category.values_list('subcategory', flat=True).distinct()
            subcategories = Subcategory.objects.filter(id__in=[sid for sid in subcat_ids if sid])
            cat_list.append({'category': category, 'subcategories': subcategories})
        tree.append({'project': project, 'categories': cat_list})

    context = {
        'tree': tree,
        'project_form': project_form,
        'category_form': category_form,
        'subcategory_form': subcategory_form,
    }
    return render(request, 'categories.html', context)