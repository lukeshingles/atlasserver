# Generated by Django 3.1.2 on 2020-11-20 16:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('forcephot', '0028_task_send_email'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='send_email',
            field=models.BooleanField(default=True, verbose_name='Email me when completed'),
        ),
    ]
